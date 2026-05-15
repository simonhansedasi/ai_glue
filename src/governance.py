import re
import json
import yaml
from pathlib import Path

from .store import get_conn

CONFIG_PATH = Path(__file__).parent.parent / "governance.yaml"

_PII_PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "email"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "ssn"),
    (re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"), "phone"),
    (re.compile(r"\b4[0-9]{12}(?:[0-9]{3})?\b"), "credit_card"),
]


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


def get_teams() -> dict:
    return _load_config().get("teams", {})


def check(provider, model, project, session_id, prompt):
    """
    Enforce governance rules. Returns a list of warning flags.
    Raises ValueError to hard-block a call before it is made.
    """
    config = _load_config()
    flags = []

    # Model allowlist — raises to block
    allowlist = config.get("model_allowlist")
    if allowlist and model not in allowlist:
        raise ValueError(f"Model '{model}' is not in the governance allowlist.")

    # PII detection — flags but does not block
    if config.get("pii_detection", True):
        pii_allowlist = set(config.get("pii_allowlist") or [])
        for pattern, label in _PII_PATTERNS:
            matches = pattern.findall(prompt)
            if matches and any(m not in pii_allowlist for m in matches):
                flags.append(f"pii:{label}")

    proj_config = config.get("projects", {}).get(project, {})

    # Daily cost cap — raises to block
    cap = proj_config.get("daily_cost_cap_usd")
    if cap is not None:
        with get_conn() as conn:
            row = conn.execute("""
                SELECT COALESCE(SUM(cost_usd), 0) AS today_cost
                FROM llm_calls
                WHERE project = ? AND date(ts) = date('now')
            """, (project,)).fetchone()
            if row["today_cost"] >= cap:
                raise ValueError(
                    f"Daily cost cap of ${cap:.2f} reached for project '{project}'."
                )

    # Rate limit — raises to block
    rate_limit = proj_config.get("rate_limit_per_hour")
    if rate_limit is not None:
        with get_conn() as conn:
            row = conn.execute("""
                SELECT COUNT(*) AS cnt FROM llm_calls
                WHERE session_id = ? AND ts >= datetime('now', '-1 hour')
            """, (session_id,)).fetchone()
            if row["cnt"] >= rate_limit:
                raise ValueError(
                    f"Rate limit of {rate_limit} calls/hour exceeded for session '{session_id}'."
                )

    return flags
