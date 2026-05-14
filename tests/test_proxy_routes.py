"""
Tests for the reverse proxy routes.
No real API calls — upstream requests are mocked.
Run: pytest tests/ -v
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ["AIGLUE_DB"] = "/tmp/aiglue_test_proxy.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.store import init_db, get_conn
import app as flask_app

init_db()

@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


def _mock_upstream(status=200, body=None, headers=None):
    resp = MagicMock()
    resp.status_code = status
    resp.content = (body or b'{"choices":[{"message":{"content":"Hello"}}],"usage":{"prompt_tokens":10,"completion_tokens":5}}')
    resp.headers = headers or {"content-type": "application/json"}
    resp.iter_content = MagicMock(return_value=iter([resp.content]))
    return resp


# ── OpenAI proxy ──────────────────────────────────────────────────────────────

def test_openai_proxy_logs_call(client):
    upstream = _mock_upstream()
    with patch("requests.request", return_value=upstream):
        with patch("src.governance.check", return_value=[]):
            resp = client.post(
                "/proxy/openai/v1/chat/completions",
                data=json.dumps({
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "Hello from proxy"}],
                }),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer test-key",
                    "X-Aiglue-Session": "proxy-test-session",
                    "X-Aiglue-Project": "proxy-test-project",
                },
            )

    assert resp.status_code == 200

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM llm_calls WHERE session_id='proxy-test-session' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert row is not None
    assert row["model"] == "gpt-4o"
    assert row["project"] == "proxy-test-project"
    assert row["provider"] == "openai"
    assert row["input_tokens"] == 10
    assert row["output_tokens"] == 5


def test_openai_proxy_governance_block(client):
    with patch("src.governance.check", side_effect=ValueError("Model not allowed")):
        resp = client.post(
            "/proxy/openai/v1/chat/completions",
            data=json.dumps({"model": "gpt-3.5-turbo-instruct", "messages": []}),
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-key"},
        )

    assert resp.status_code == 403
    assert b"governance_error" in resp.data


def test_openai_proxy_pii_flagged_but_allowed(client):
    upstream = _mock_upstream()
    with patch("requests.request", return_value=upstream):
        with patch("src.governance.check", return_value=["pii:email"]):
            resp = client.post(
                "/proxy/openai/v1/chat/completions",
                data=json.dumps({
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "email ceo@corp.com"}],
                }),
                headers={"Content-Type": "application/json", "Authorization": "Bearer key"},
            )

    assert resp.status_code == 200
    with get_conn() as conn:
        row = conn.execute(
            "SELECT gov_flags FROM llm_calls WHERE gov_flags LIKE '%pii%' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert "pii:email" in row["gov_flags"]


# ── Anthropic proxy ───────────────────────────────────────────────────────────

def test_anthropic_proxy_logs_call(client):
    upstream = _mock_upstream(body=b'{"content":[{"text":"Hi"}],"usage":{"input_tokens":8,"output_tokens":3}}')
    with patch("requests.request", return_value=upstream):
        with patch("src.governance.check", return_value=[]):
            resp = client.post(
                "/proxy/anthropic/v1/messages",
                data=json.dumps({
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": "Hello from anthropic proxy"}],
                }),
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": "test-key",
                    "anthropic-version": "2023-06-01",
                    "X-Aiglue-Session": "anthropic-proxy-session",
                    "X-Aiglue-Project": "anthropic-proxy-project",
                },
            )

    assert resp.status_code == 200

    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM llm_calls WHERE session_id='anthropic-proxy-session' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert row is not None
    assert row["model"] == "claude-sonnet-4-6"
    assert row["provider"] == "anthropic"
    assert row["input_tokens"] == 8
    assert row["output_tokens"] == 3


def test_anthropic_proxy_governance_block(client):
    with patch("src.governance.check", side_effect=ValueError("Model not allowed")):
        resp = client.post(
            "/proxy/anthropic/v1/messages",
            data=json.dumps({"model": "some-banned-model", "messages": []}),
            headers={"Content-Type": "application/json", "x-api-key": "test-key"},
        )

    assert resp.status_code == 403


# ── Auto-detection ───────────────────────────────────────────────────────────

def test_project_detected_from_tool_use(client):
    upstream = _mock_upstream(body=b'{"content":[{"text":"Hi"}],"usage":{"input_tokens":5,"output_tokens":2}}')
    with patch("requests.request", return_value=upstream):
        with patch("src.governance.check", return_value=[]):
            resp = client.post(
                "/proxy/anthropic/v1/messages",
                data=json.dumps({
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 100,
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": [
                            {"type": "tool_use", "id": "t1", "name": "Read",
                             "input": {"file_path": "/home/simonhans/coding/rippleforge/app.py"}}
                        ]},
                        {"role": "user", "content": [
                            {"type": "tool_result", "tool_use_id": "t1",
                             "content": [{"type": "text", "text": "...file contents..."}]}
                        ]},
                    ],
                }),
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": "sk-ant-test",
                    "X-Aiglue-Session": "detect-project-session",
                },
            )

    assert resp.status_code == 200
    with get_conn() as conn:
        row = conn.execute(
            "SELECT project FROM llm_calls WHERE session_id='detect-project-session' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["project"] == "rippleforge"


def test_session_stable_within_conversation(client):
    upstream = _mock_upstream(body=b'{"content":[{"text":"Hi"}],"usage":{"input_tokens":5,"output_tokens":2}}')
    first_msg = "What is the capital of France?"
    second_call_messages = [
        {"role": "user", "content": first_msg},
        {"role": "assistant", "content": "Paris."},
        {"role": "user", "content": "And Germany?"},
    ]

    with patch("requests.request", return_value=upstream):
        with patch("src.governance.check", return_value=[]):
            client.post(
                "/proxy/anthropic/v1/messages",
                data=json.dumps({
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": first_msg}],
                }),
                headers={"Content-Type": "application/json", "x-api-key": "sk-ant-test",
                         "X-Aiglue-Session": "turn1-marker"},
            )
            client.post(
                "/proxy/anthropic/v1/messages",
                data=json.dumps({
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 100,
                    "messages": second_call_messages,
                }),
                headers={"Content-Type": "application/json", "x-api-key": "sk-ant-test",
                         "X-Aiglue-Session": "turn2-marker"},
            )

    # Both calls should produce the same auto-detected session (hash of first message)
    from routes.proxy import _detect_session
    expected = _detect_session(json.dumps({"messages": [{"role": "user", "content": first_msg}]}).encode())
    assert expected is not None
    expected2 = _detect_session(json.dumps({"messages": second_call_messages}).encode())
    assert expected == expected2


def test_header_takes_precedence_over_autodetect(client):
    upstream = _mock_upstream(body=b'{"content":[{"text":"Hi"}],"usage":{"input_tokens":5,"output_tokens":2}}')
    with patch("requests.request", return_value=upstream):
        with patch("src.governance.check", return_value=[]):
            resp = client.post(
                "/proxy/anthropic/v1/messages",
                data=json.dumps({
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 100,
                    "system": "Contents of /home/simonhans/coding/rippleforge/CLAUDE.md\n(stuff)",
                    "messages": [{"role": "user", "content": "Hello"}],
                }),
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": "sk-ant-test",
                    "X-Aiglue-Project": "explicit-override",
                    "X-Aiglue-Session": "precedence-session",
                },
            )

    assert resp.status_code == 200
    with get_conn() as conn:
        row = conn.execute(
            "SELECT project FROM llm_calls WHERE session_id='precedence-session' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["project"] == "explicit-override"


# ── Team key resolution ───────────────────────────────────────────────────────

def test_anthropic_team_key_tags_project_and_swaps_key(client):
    upstream = _mock_upstream(body=b'{"content":[{"text":"Hi"}],"usage":{"input_tokens":8,"output_tokens":3}}')
    mock_req = MagicMock(return_value=upstream)

    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-real-key"
    with patch("requests.request", mock_req):
        with patch("src.governance.check", return_value=[]):
            with patch("routes.proxy.get_teams", return_value={"sk-aiglue-eng": {"name": "engineering"}}):
                resp = client.post(
                    "/proxy/anthropic/v1/messages",
                    data=json.dumps({
                        "model": "claude-sonnet-4-6",
                        "max_tokens": 100,
                        "messages": [{"role": "user", "content": "Hello"}],
                    }),
                    headers={
                        "Content-Type": "application/json",
                        "x-api-key": "sk-aiglue-eng-secret123",
                        "anthropic-version": "2023-06-01",
                        "X-Aiglue-Session": "team-anthropic-session",
                    },
                )

    assert resp.status_code == 200
    fwd_hdrs = mock_req.call_args[1]["headers"]
    forwarded_key = next((v for k, v in fwd_hdrs.items() if k.lower() == "x-api-key"), "")
    assert forwarded_key == "sk-ant-real-key"

    with get_conn() as conn:
        row = conn.execute(
            "SELECT project FROM llm_calls WHERE session_id='team-anthropic-session' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["project"] == "engineering"


def test_openai_team_key_tags_project_and_swaps_key(client):
    upstream = _mock_upstream()
    mock_req = MagicMock(return_value=upstream)

    os.environ["OPENAI_API_KEY"] = "sk-openai-real-key"
    with patch("requests.request", mock_req):
        with patch("src.governance.check", return_value=[]):
            with patch("routes.proxy.get_teams", return_value={"sk-aiglue-marketing": {"name": "marketing"}}):
                resp = client.post(
                    "/proxy/openai/v1/chat/completions",
                    data=json.dumps({
                        "model": "gpt-4o",
                        "messages": [{"role": "user", "content": "Hello"}],
                    }),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer sk-aiglue-marketing-secret456",
                        "X-Aiglue-Session": "team-openai-session",
                    },
                )

    assert resp.status_code == 200
    forwarded_auth = mock_req.call_args[1]["headers"].get("Authorization", "")
    assert forwarded_auth == "Bearer sk-openai-real-key"

    with get_conn() as conn:
        row = conn.execute(
            "SELECT project FROM llm_calls WHERE session_id='team-openai-session' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["project"] == "marketing"


# ── Stream parsing ────────────────────────────────────────────────────────────

def test_parse_openai_stream():
    from routes.proxy import _parse_openai_stream
    chunks = [
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
        b'data: {"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n',
        b'data: [DONE]\n\n',
    ]
    text, inp, out = _parse_openai_stream(chunks)
    assert text == "Hello world"
    assert inp == 5
    assert out == 2


def test_parse_anthropic_stream():
    from routes.proxy import _parse_anthropic_stream
    chunks = [
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":10}}}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}\n\n',
        b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":4}}\n\n',
    ]
    text, inp, out, tool_calls = _parse_anthropic_stream(chunks)
    assert text == "Hi"
    assert inp == 10
    assert out == 4
    assert tool_calls == []
