"""Cost table: current prices, longest-prefix matching, cache-aware math, and the proxy logging cache tokens + cost."""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ["AIGLUE_DB"] = "/tmp/aiglue_test_costs.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.costs import COSTS, estimate_cost
from src.store import init_db, get_conn
import app as flask_app

init_db()


@pytest.mark.parametrize("model,inp,out", [
    ("claude-fable-5-1", 10, 50), ("claude-fable-5", 10, 50), ("claude-opus-5-5", 4, 20), ("claude-opus-5", 5, 25),
    ("claude-opus-4-7", 5, 25), ("claude-sonnet-5-5", 2, 10), ("claude-sonnet-5", 2, 10), ("claude-sonnet-4-6", 3, 15),
    ("claude-haiku-5-5", 0.10, 0.50), ("claude-haiku-4-5", 1, 5),
])
def test_current_prices(model, inp, out):
    assert estimate_cost(model, 1_000_000, 0) == pytest.approx(inp)
    assert estimate_cost(model, 0, 1_000_000) == pytest.approx(out)


def test_every_model_the_governance_allowlist_permits_is_priced():
    import yaml
    allowed = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "governance.yaml")))["model_allowlist"]
    unpriced = [m for m in allowed if estimate_cost(m, 1000, 1000) == 0.0]
    assert not unpriced, f"allowed but priced at $0: {unpriced}"


def test_versioned_ids_use_the_longest_matching_key():
    assert estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 0) == pytest.approx(1.0)
    # 'claude-sonnet-5-5-<date>' must not fall onto the shorter 'claude-sonnet-5' (same price today, but not by design)
    assert estimate_cost("claude-opus-5-5-20270101", 1_000_000, 0) == pytest.approx(4.0)   # not claude-opus-5 ($5)
    assert estimate_cost("claude-opus-5-20270101", 1_000_000, 0) == pytest.approx(5.0)


def test_unknown_model_is_zero():
    assert estimate_cost("some-mystery-model", 1000, 1000, 500, 500) == 0.0


def test_cache_tokens_are_billed_separately():
    # haiku-4-5: input $1, output $5; cache read 0.1x input, cache write 1.25x input
    assert estimate_cost("claude-haiku-4-5", 100, 10, cache_read_tokens=1000, cache_write_tokens=200) == pytest.approx(
        (100 * 1 + 10 * 5 + 1000 * 0.1 + 200 * 1.25) / 1e6)
    # models with an explicit cache-read price use it
    assert estimate_cost("claude-fable-5-1", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.25)
    assert estimate_cost("claude-opus-5-5", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.20)


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


def _post(client, session, body, upstream):
    with patch("requests.request", return_value=upstream), patch("routes.proxy.check", return_value=[]):
        r = client.post("/proxy/anthropic/v1/messages", data=json.dumps(body),
                        headers={"Content-Type": "application/json", "x-api-key": "k", "X-Aiglue-Session": session,
                                 "X-Aiglue-Project": "cost-test"})
        r.get_data()  # drain: a streamed response logs when its generator finishes
    with get_conn() as conn:
        return conn.execute("SELECT * FROM llm_calls WHERE session_id=?", (session,)).fetchone()


def test_non_streaming_call_logs_cache_tokens_and_cost(client):
    usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200}
    up = MagicMock(status_code=200, headers={"content-type": "application/json"},
                   content=json.dumps({"content": [{"type": "text", "text": "ok"}], "usage": usage}).encode())
    row = _post(client, "cost-ns", {"model": "claude-haiku-4-5-20251001", "max_tokens": 5,
                                    "messages": [{"role": "user", "content": "hi"}]}, up)
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"], row["cache_write_tokens"]) == (10, 5, 1000, 200)
    assert row["cost_usd"] == pytest.approx((10 * 1 + 5 * 5 + 1000 * 0.1 + 200 * 1.25) / 1e6)


def test_streaming_call_logs_cache_tokens_and_cost(client):
    events = [
        'event: message_start\ndata: ' + json.dumps({"type": "message_start", "message": {"usage": {
            "input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0}}}) + '\n\n',
        'event: content_block_delta\ndata: ' + json.dumps({"type": "content_block_delta", "index": 0,
                                                           "delta": {"type": "text_delta", "text": "ok"}}) + '\n\n',
        'event: message_delta\ndata: ' + json.dumps({"type": "message_delta", "usage": {"output_tokens": 5}}) + '\n\n',
    ]
    chunks = [e.encode() for e in events]
    up = MagicMock(status_code=200, headers={"content-type": "text/event-stream"})
    up.iter_content = MagicMock(return_value=iter(chunks))
    row = _post(client, "cost-st", {"model": "claude-haiku-4-5-20251001", "max_tokens": 5, "stream": True,
                                    "messages": [{"role": "user", "content": "hi"}]}, up)
    assert (row["input_tokens"], row["output_tokens"], row["cache_read_tokens"]) == (10, 5, 1000)
    assert row["cost_usd"] == pytest.approx((10 * 1 + 5 * 5 + 1000 * 0.1) / 1e6)
