"""
Run: pytest tests/ -v
No API keys required — all LLM calls are mocked.
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Use a temp file DB so each test run starts clean
os.environ["AIGLUE_DB"] = "/tmp/aiglue_test.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.store import init_db, get_conn

init_db()


def _mock_anthropic_response(text="Test response", input_tokens=10, output_tokens=20):
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    resp.usage = MagicMock(input_tokens=input_tokens, output_tokens=output_tokens)
    return resp


def _mock_openai_response(text="Test response", prompt_tokens=10, completion_tokens=20):
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=text))]
    resp.usage = MagicMock(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    return resp


# ── GluedClient logging ──────────────────────────────────────────────────────

def test_anthropic_call_is_logged():
    mock_anthropic = MagicMock()
    mock_anthropic.Anthropic.return_value.messages.create.return_value = (
        _mock_anthropic_response()
    )
    with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
        with patch("src.governance.check", return_value=[]):
            from src.proxy import GluedClient
            client = GluedClient("anthropic", session_id="s-test", project="p-test")
            client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=50,
                messages=[{"role": "user", "content": "Hello"}],
            )

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM llm_calls WHERE session_id='s-test' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert row is not None
    assert row["model"] == "claude-sonnet-4-6"
    assert row["project"] == "p-test"
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 20
    assert row["error"] is None


def test_openai_call_is_logged():
    with patch("openai.OpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create.return_value = _mock_openai_response()
        with patch("src.governance.check", return_value=[]):
            from src.proxy import GluedClient
            client = GluedClient("openai", session_id="s-oai", project="p-oai")
            client.chat.completions.create(
                model="gpt-4o",
                max_tokens=50,
                messages=[{"role": "user", "content": "Hello"}],
            )

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM llm_calls WHERE session_id='s-oai' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert row is not None
    assert row["model"] == "gpt-4o"
    assert row["input_tokens"] == 10


# ── Governance ───────────────────────────────────────────────────────────────

def test_governance_blocks_unapproved_model():
    from src.governance import check
    with patch("src.governance._load_config", return_value={
        "model_allowlist": ["claude-sonnet-4-6"],
        "pii_detection": False,
    }):
        with pytest.raises(ValueError, match="allowlist"):
            check("openai", "gpt-3.5-turbo-instruct", "proj", "sess", "Hello")


def test_pii_flags_email():
    from src.governance import check
    with patch("src.governance._load_config", return_value={"pii_detection": True}):
        flags = check("anthropic", "claude-sonnet-4-6", "proj", "sess",
                      "Send this to ceo@company.com please")
    assert "pii:email" in flags


def test_pii_flags_ssn():
    from src.governance import check
    with patch("src.governance._load_config", return_value={"pii_detection": True}):
        flags = check("anthropic", "claude-sonnet-4-6", "proj", "sess",
                      "SSN on file: 123-45-6789")
    assert "pii:ssn" in flags


def test_pii_allowlisted_email_not_flagged():
    from src.governance import check
    with patch("src.governance._load_config", return_value={
        "pii_detection": True,
        "pii_allowlist": ["woses21@gmail.com"],
    }):
        flags = check("anthropic", "claude-sonnet-4-6", "proj", "sess",
                      "userEmail: woses21@gmail.com")
    assert flags == []


def test_pii_non_allowlisted_email_still_flagged():
    from src.governance import check
    with patch("src.governance._load_config", return_value={
        "pii_detection": True,
        "pii_allowlist": ["woses21@gmail.com"],
    }):
        flags = check("anthropic", "claude-sonnet-4-6", "proj", "sess",
                      "Contact ceo@company.com for details")
    assert "pii:email" in flags


def test_pii_mixed_prompt_flags_non_allowlisted():
    from src.governance import check
    with patch("src.governance._load_config", return_value={
        "pii_detection": True,
        "pii_allowlist": ["woses21@gmail.com"],
    }):
        flags = check("anthropic", "claude-sonnet-4-6", "proj", "sess",
                      "From woses21@gmail.com to ceo@company.com")
    assert "pii:email" in flags


def test_clean_prompt_no_flags():
    from src.governance import check
    with patch("src.governance._load_config", return_value={"pii_detection": True}):
        flags = check("anthropic", "claude-sonnet-4-6", "proj", "sess",
                      "Summarize the Q2 earnings report.")
    assert flags == []


# ── Cost estimation ──────────────────────────────────────────────────────────

def test_cost_estimation():
    from src.costs import estimate_cost
    cost = estimate_cost("claude-sonnet-4-6", 1000, 500)
    # 1000 * 3.00 / 1M + 500 * 15.00 / 1M = 0.003 + 0.0075 = 0.0105
    assert abs(cost - 0.0105) < 0.0001


def test_unknown_model_returns_zero():
    from src.costs import estimate_cost
    assert estimate_cost("some-mystery-model", 1000, 1000) == 0.0
