import hashlib
import json
import os

from .store import get_conn
from .costs import estimate_cost

LOG_RAW = os.getenv("AIGLUE_LOG_RAW", "true").lower() == "true"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def log_call(
    provider: str,
    model: str,
    session_id: str,
    project: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    raw_prompt: str,
    raw_response: str,
    gov_flags: list,
    error: str = None,
    tool_calls: list = None,
):
    cost = estimate_cost(model, input_tokens, output_tokens)
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO llm_calls
                (provider, model, session_id, project, input_tokens, output_tokens,
                 cost_usd, latency_ms, prompt_hash, raw_prompt, raw_response, gov_flags, error, tool_calls)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            provider, model, session_id, project,
            input_tokens, output_tokens, cost, latency_ms,
            _hash(raw_prompt),
            raw_prompt if LOG_RAW else None,
            raw_response if LOG_RAW else None,
            json.dumps(gov_flags),
            error,
            json.dumps(tool_calls) if tool_calls else None,
        ))
        conn.commit()
