"""Agent staging: one test per health rule (src/staging.py), then the pages."""
import base64
import os
import sys
import uuid
from datetime import datetime, timedelta

import pytest

os.environ["AIGLUE_DB"] = "/tmp/aiglue_test_staging.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src import staging as st
from src.store import init_db, get_conn
import app as flask_app

init_db()
NOW = datetime(2026, 10, 13, 12, 0, 0)


def row(agent="chat", parent="", h="hash_live", calls=100, errors=0, calls_7d=100, errors_7d=0, project="p", last="2026-10-13 11:00:00"):
    return {"project": project, "agent": agent, "parent_agent": parent, "config_hash": h, "calls": calls, "errors": errors,
            "calls_7d": calls_7d, "errors_7d": errors_7d, "cost_usd": 1.0, "avg_latency_ms": 1000,
            "first_ts": "2026-10-01 00:00:00", "last_ts": last}


def ev(kind="baseline", decision=None, h="hash_live", ts="2026-10-12 00:00:00", agent="chat", project="p", overall=0.97):
    return {"run_id": uuid.uuid4().hex, "ts": ts, "project": project, "agent": agent, "config_hash": h, "kind": kind,
            "decision": decision, "overall": overall, "n_runs": 10, "has_report": 1}


def status_of(rows, evals, agent="chat", project="p"):
    model = st.build(rows, evals, NOW)
    p = next(x for x in model["projects"] if x["name"] == project)
    return next(a for a in p["agents"] if a["name"] == agent)


# ---- RED rules -------------------------------------------------------------------------------------------------

def test_red_when_latest_gate_blocked():
    a = status_of([row()], [ev(), ev("gate", "BLOCK", h="cand1234", ts="2026-10-12 06:00:00")])
    assert a["status"] == st.RED and "BLOCK" in a["reasons"][0] and "cand1234"[:8] in a["reasons"][0]


def test_red_when_latest_gate_invalid():
    assert status_of([row()], [ev(), ev("gate", "INVALID", ts="2026-10-12 06:00:00")])["status"] == st.RED


def test_blocked_gate_is_cleared_by_a_later_pass():
    evals = [ev(), ev("gate", "BLOCK", ts="2026-10-12 01:00:00"), ev("gate", "PASS", ts="2026-10-12 09:00:00")]
    assert status_of([row()], evals)["status"] == st.GREEN


def test_red_on_high_production_error_rate():
    a = status_of([row(calls_7d=100, errors_7d=6)], [ev()])  # 6% > 5%
    assert a["status"] == st.RED and "6%" in a["reasons"][0]


def test_error_rate_needs_enough_calls_and_exceeding_the_limit():
    assert status_of([row(calls_7d=19, errors_7d=19)], [ev()])["status"] == st.GREEN      # too few calls to judge
    assert status_of([row(calls_7d=100, errors_7d=5)], [ev()])["status"] == st.GREEN      # exactly 5% is not over


def test_red_when_evals_exist_but_no_baseline():
    a = status_of([row()], [ev("gate", "PASS")])
    assert a["status"] == st.RED and any("no baseline" in r for r in a["reasons"])


# ---- AMBER rules -----------------------------------------------------------------------------------------------

def test_amber_when_production_runs_unvetted_config():
    a = status_of([row(h="hash_live"), row(h="newhash9", calls_7d=5)], [ev(h="hash_live")])
    assert a["status"] == st.AMBER and "newhash9"[:8] in a["reasons"][0]
    assert {v["config_hash"]: v["vetted"] for v in a["versions"]} == {"hash_live": True, "newhash9": False}


def test_old_unvetted_config_with_no_recent_calls_is_not_live():
    assert status_of([row(h="old_one", calls_7d=0), row(h="hash_live")], [ev()])["status"] == st.GREEN


def test_gate_pass_counts_as_vetted():
    evals = [ev(h="hash_live"), ev("gate", "PASS", h="newhash9", ts="2026-10-12 09:00:00")]
    assert status_of([row(h="newhash9")], evals)["status"] == st.GREEN


def test_amber_when_last_eval_is_stale():
    a = status_of([row()], [ev(ts="2026-09-20 00:00:00")])  # 23 days before NOW
    assert a["status"] == st.AMBER and "days old" in a["reasons"][0]


def test_same_second_runs_use_insertion_order_so_a_later_pass_clears_an_earlier_block():
    block = dict(ev("gate", "BLOCK", ts="2026-10-12 06:00:00"), seq=1)
    passed = dict(ev("gate", "PASS", ts="2026-10-12 06:00:00"), seq=2)
    for order in ([block, ev(), passed], [passed, ev(), block]):  # input order must not matter
        a = status_of([row()], order)
        assert a["status"] == st.GREEN
        assert [e["decision"] for e in a["evals"]][:2] == ["PASS", "BLOCK"]  # newest first


# ---- GREEN / UNTESTED ------------------------------------------------------------------------------------------

def test_green_when_baselined_live_and_healthy():
    a = status_of([row()], [ev()])
    assert a["status"] == st.GREEN and a["reasons"] == []


def test_untested_when_calls_but_no_evals():
    a = status_of([row(agent="recap")], [], agent="recap")
    assert a["status"] == st.UNTESTED


def test_untested_agent_with_high_error_rate_is_still_red():
    assert status_of([row(calls_7d=100, errors_7d=50)], [])["status"] == st.RED


# ---- roll-up, tree, merging --------------------------------------------------------------------------------------

def _tree_rows():
    return [row("chat"), row("parser", parent="chat", h="p1", calls_7d=100, errors_7d=40), row("recap", h="r1")]


def test_parent_rolls_up_worst_child_status():
    evals = [ev(agent="chat"), ev(agent="parser", h="p1")]
    model = st.build(_tree_rows(), evals, NOW)
    agents = {a["name"]: a for a in model["projects"][0]["agents"]}
    assert agents["parser"]["status"] == st.RED
    assert agents["chat"]["status"] == st.GREEN and agents["chat"]["rollup"] == st.RED
    assert model["projects"][0]["status"] == st.RED


def test_untested_child_does_not_change_rollup_but_is_counted():
    model = st.build([row("chat"), row("parser", parent="chat", h="p1")], [ev(agent="chat")], NOW)
    chat = next(a for a in model["projects"][0]["agents"] if a["name"] == "chat")
    assert chat["status"] == st.GREEN and chat["rollup"] == st.GREEN and chat["untested_below"] == 1


def test_tree_nests_subagents_and_lists_direct_agent_at_root():
    rows = [row("chat"), row("parser", parent="chat", h="p1"), row("parser", parent="", h="p1")]  # called both ways
    tree = st.build(rows, [], NOW)["projects"][0]["tree"]
    assert {n["name"] for n in tree} == {"chat", "parser"}
    assert [c["name"] for c in next(n for n in tree if n["name"] == "chat")["children"]] == ["parser"]


def test_cycle_does_not_recurse_forever():
    rows = [row("a", parent="b"), row("b", parent="a")]
    assert st.build(rows, [], NOW)["projects"][0]["n_agents"] == 2


def test_unlabelled_traffic_is_not_an_agent_and_alone_does_not_make_a_project():
    model = st.build([row(st.UNLABELLED, project="devnoise"), row("chat", project="real"),
                      row(st.UNLABELLED, project="real", calls=7, calls_7d=3)], [], NOW)
    assert [p["name"] for p in model["projects"]] == ["real"]
    assert model["projects"][0]["unlabelled"] == {"calls": 7, "calls_7d": 3}
    assert [a["name"] for a in model["projects"][0]["agents"]] == ["chat"]


def test_project_with_only_eval_runs_still_appears():
    model = st.build([], [ev(project="evalonly")], NOW)
    assert model["projects"][0]["name"] == "evalonly" and model["projects"][0]["agents"][0]["status"] == st.GREEN


def test_rows_from_several_instances_are_merged():
    merged = st.merge_rows([row(calls=10, calls_7d=10, errors_7d=1), row(calls=30, calls_7d=30, errors_7d=2, last="2026-10-13 11:30:00")])
    assert len(merged) == 1 and merged[0]["calls"] == 40 and merged[0]["errors_7d"] == 3
    assert merged[0]["last_ts"] == "2026-10-13 11:30:00" and merged[0]["avg_latency_ms"] == 1000


def test_worst_project_sorts_first():
    rows = [row("a", project="healthy"), row("a", project="sick", calls_7d=100, errors_7d=90)]
    evals = [ev(agent="a", project="healthy"), ev(agent="a", project="sick")]
    assert [p["name"] for p in st.build(rows, evals, NOW)["projects"]] == ["sick", "healthy"]


# ---- pages --------------------------------------------------------------------------------------------------------

AUTH = {"Authorization": "Basic " + base64.b64encode(b"x:tok").decode()}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AIGLUE_TOKEN", "tok")
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


@pytest.fixture
def seeded():
    """A uniquely-named project with a labelled parent+subagent in production and one posted baseline."""
    proj = "pg" + uuid.uuid4().hex[:8]
    with get_conn() as conn:
        for agent, parent, n in (("chat", None, 30), ("parser", "chat", 30)):
            for i in range(n):
                conn.execute("INSERT INTO llm_calls (provider, model, session_id, project, input_tokens, output_tokens, cost_usd, "
                             "latency_ms, config_hash, agent, parent_agent, error) VALUES ('anthropic','m','s1',?,1,1,0.01,500,?,?,?,?)",
                             (proj, "hh_" + agent, agent, parent, "boom" if (agent == "parser" and i < 10) else None))
        conn.commit()
    return proj


def test_pages_render_with_data(client, seeded):
    client.post("/api/eval_runs", json={"project": seeded, "agent": "chat", "config_hash": "hh_chat", "kind": "baseline",
                                        "cases": [{"case": "a", "passes": 10, "n": 10}], "report_html": "<html>r</html>"}, headers=AUTH)
    home = client.get("/staging", headers=AUTH)
    assert home.status_code == 200 and seeded.encode() in home.data
    proj = client.get(f"/staging/{seeded}", headers=AUTH)
    assert proj.status_code == 200 and b"chat" in proj.data and b"parser" in proj.data
    assert b"via subagent" in proj.data  # chat is GREEN but rolls up its failing subagent
    agent = client.get(f"/staging/{seeded}/chat", headers=AUTH)
    assert agent.status_code == 200 and b"hh_chat" in agent.data and b"Eval history" in agent.data and b"/report" in agent.data
    assert client.get(f"/staging/{seeded}/parser", headers=AUTH).status_code == 200


def test_unknown_project_and_agent_are_404(client, seeded):
    assert client.get("/staging/nope-" + uuid.uuid4().hex, headers=AUTH).status_code == 404
    assert client.get(f"/staging/{seeded}/nope", headers=AUTH).status_code == 404


def test_pages_need_credentials_when_token_set(client, seeded):
    for path in ("/staging", f"/staging/{seeded}", "/api/staging"):
        assert client.get(path).status_code == 401


def test_agent_names_are_escaped_in_pages(client):
    proj = "esc" + uuid.uuid4().hex[:6]
    with get_conn() as conn:
        conn.execute("INSERT INTO llm_calls (provider, model, session_id, project, agent, config_hash) VALUES ('a','m','s',?,?,?)",
                     (proj, "<script>alert(1)</script>", "h"))
        conn.commit()
    body = client.get(f"/staging/{proj}", headers=AUTH).data
    assert b"<script>alert(1)</script>" not in body and b"&lt;script&gt;" in body


def test_eval_session_traffic_is_excluded_from_production_stats(client):
    proj = "evx" + uuid.uuid4().hex[:6]
    with get_conn() as conn:
        for sess in ("eval", "eval", "real"):
            conn.execute("INSERT INTO llm_calls (provider, model, session_id, project, agent, config_hash) VALUES ('a','m',?,?,?,?)",
                         (sess, proj, "chat", "h"))
        conn.commit()
    data = client.get("/api/staging", headers=AUTH).get_json()
    agent = next(a for p in data["projects"] if p["name"] == proj for a in p["agents"])
    assert agent["calls"] == 1


# ---- behaviour drift (production vs staging tool mix) -------------------------------------------------------------

def mixrow(kind, counts, h="hash_live", calls=None, agent="chat", project="p"):
    return {"project": project, "agent": agent, "config_hash": h, "kind": kind,
            "calls": calls if calls is not None else sum(counts.values()), "counts": counts}


STAGING = mixrow("eval", {"propose": 40, "revise": 40, "(no tool)": 20})


def test_tvd_known_values():
    assert st.tvd({"a": 1, "b": 1}, {"a": 1, "b": 1}) == 0
    assert st.tvd({"a": 5}, {"b": 5}) == 1
    assert st.tvd({"x": 1, "y": 1}, {"x": 3, "y": 1}) == pytest.approx(0.25)
    assert st.tvd({}, {"a": 1}) is None


def test_drift_verdicts():
    assert st.drift("h", mixrow("prod", {"propose": 45, "revise": 35, "(no tool)": 20}), STAGING)["verdict"] == "ok"
    far = st.drift("h", mixrow("prod", {"propose": 90, "(no tool)": 10}), STAGING)
    assert far["verdict"] == "drift" and far["tvd"] > st.DRIFT_TVD
    assert st.drift("h", mixrow("prod", {"propose": 9, "(no tool)": 1}), STAGING)["verdict"] == "insufficient"   # too few prod calls
    assert st.drift("h", mixrow("prod", {"propose": 90, "(no tool)": 10}), mixrow("eval", {"propose": 5}))["verdict"] == "insufficient"
    assert st.drift("h", mixrow("prod", {"propose": 90}), None)["verdict"] == "no_staging"
    assert st.drift("h", None, STAGING)["verdict"] == "no_staging"


def test_drift_rows_show_both_shares_largest_first():
    d = st.drift("h", mixrow("prod", {"propose": 90, "(no tool)": 10}), STAGING)
    assert d["rows"][0]["tool"] == "propose" and d["rows"][0]["prod"] == pytest.approx(0.9) and d["rows"][0]["staging"] == pytest.approx(0.4)
    assert {r["tool"] for r in d["rows"]} == {"propose", "revise", "(no tool)"}


def _drift_agent(prod_counts, evals=None, rows=None):
    model = st.build(rows or [row()], evals if evals is not None else [ev()], NOW,
                     tool_mix=[mixrow("prod", prod_counts), STAGING])
    return next(a for a in model["projects"][0]["agents"] if a["name"] == "chat")


def test_amber_when_production_mix_drifts_from_staging():
    a = _drift_agent({"propose": 90, "(no tool)": 10})
    assert a["status"] == st.AMBER and "tool-call mix differs" in a["reasons"][0] and a["drift"][0]["verdict"] == "drift"


def test_no_amber_when_mix_is_within_limit_or_sample_is_small():
    assert _drift_agent({"propose": 45, "revise": 35, "(no tool)": 20})["status"] == st.GREEN
    assert _drift_agent({"propose": 9, "(no tool)": 1})["status"] == st.GREEN


def test_untested_agent_is_not_made_amber_by_drift():
    assert _drift_agent({"propose": 90, "(no tool)": 10}, evals=[])["status"] == st.UNTESTED


def test_drift_only_computed_for_configs_live_in_the_last_7_days():
    a = _drift_agent({"propose": 90, "(no tool)": 10}, rows=[row(h="hash_live", calls_7d=0), row(h="other", calls_7d=0)])
    assert a["drift"] == [] and a["status"] == st.GREEN


def test_merge_mix_sums_instances():
    m = st.merge_mix([mixrow("prod", {"a": 1}), mixrow("prod", {"a": 2, "b": 1})])
    assert m[("p", "chat", "hash_live", "prod")] == {"calls": 4, "counts": {"a": 3, "b": 1}}


def _insert(conn, proj, session, tool_calls, config_hash="h1", agent="chat", ts=None):
    conn.execute("INSERT INTO llm_calls (provider, model, session_id, project, agent, config_hash, tool_calls, ts) "
                 "VALUES ('a','m',?,?,?,?,?,COALESCE(?, CURRENT_TIMESTAMP))", (session, proj, agent, config_hash, tool_calls, ts))


def test_tool_mix_payload_rules(client):
    proj = "mix" + uuid.uuid4().hex[:6]
    with get_conn() as conn:
        _insert(conn, proj, "real", '[{"name": "propose"}, {"name": "revise"}]')              # prod: two tool blocks
        _insert(conn, proj, "real", None)                                                      # prod: text-only -> (no tool)
        _insert(conn, proj, "real", '[{"name": "propose"}]', ts="2020-01-01 00:00:00")        # prod but too old -> excluded
        _insert(conn, proj, "real", '[{"name": "propose"}]', config_hash=None)                # logged by old proxy code -> excluded
        _insert(conn, proj, "eval", '[{"name": "revise"}]', ts="2020-01-01 00:00:00")         # eval: all-time, old is fine
        conn.commit()
    mix = {(m["kind"]): m for m in client.get("/api/summary", headers=AUTH).get_json()["tool_mix"] if m["project"] == proj}
    assert mix["prod"] == {"project": proj, "agent": "chat", "config_hash": "h1", "kind": "prod",
                           "calls": 2, "counts": {"propose": 1, "revise": 1, "(no tool)": 1}}
    assert mix["eval"]["calls"] == 1 and mix["eval"]["counts"] == {"revise": 1}


def test_agent_page_shows_drift_table(client):
    proj = "dr" + uuid.uuid4().hex[:6]
    with get_conn() as conn:
        for _ in range(40):
            _insert(conn, proj, "real", '[{"name": "propose"}]', config_hash="hX")
            _insert(conn, proj, "eval", '[{"name": "revise"}]', config_hash="hX")
        conn.commit()
    client.post("/api/eval_runs", json={"project": proj, "agent": "chat", "config_hash": "hX", "kind": "baseline",
                                        "cases": [{"case": "a", "passes": 10, "n": 10}]}, headers=AUTH)
    page = client.get(f"/staging/{proj}/chat", headers=AUTH).data
    assert b"Behaviour drift" in page and b"DIFFERS" in page and b"100%" in page
