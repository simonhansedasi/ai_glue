"""eval_runs: posting, validation, auth, reading back."""
import base64
import os
import sys
import uuid

import pytest

os.environ["AIGLUE_DB"] = "/tmp/aiglue_test_evalruns.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.store import init_db, get_conn
import app as flask_app

init_db()
AUTH = {"Authorization": "Basic " + base64.b64encode(b"x:tok").decode()}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("AIGLUE_TOKEN", "tok")
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


def payload(**over):
    p = {"project": "proj", "agent": "chat", "config_hash": "abc123", "kind": "gate", "decision": "PASS",
         "cases": [{"case": "a", "passes": 5, "n": 5}, {"case": "b", "passes": 3, "n": 5, "errors": 1}],
         "api_calls": 10, "tokens_in": 100, "tokens_out": 20, "report_html": "<html><body>r</body></html>"}
    p.update(over)
    return p


def test_write_refused_when_token_not_configured(monkeypatch):
    monkeypatch.delenv("AIGLUE_TOKEN", raising=False)
    with flask_app.app.test_client() as c:
        before = get_conn().execute("select count(*) from eval_runs").fetchone()[0]
        assert c.post("/api/eval_runs", json=payload()).status_code == 403
        assert get_conn().execute("select count(*) from eval_runs").fetchone()[0] == before


def test_write_needs_credentials(client):
    assert client.post("/api/eval_runs", json=payload()).status_code == 401
    bad = {"Authorization": "Basic " + base64.b64encode(b"x:nope").decode()}
    assert client.post("/api/eval_runs", json=payload(), headers=bad).status_code == 401


def test_post_then_read_back(client):
    r = client.post("/api/eval_runs", json=payload(project="p1", agent="a1"), headers=AUTH)
    assert r.status_code == 201
    run_id = r.get_json()["run_id"]
    run = client.get(f"/api/eval_runs/{run_id}", headers=AUTH).get_json()
    assert run["overall"] == pytest.approx(8 / 10)  # computed server-side
    assert run["n_runs"] == 10 and run["n_cases"] == 2 and run["decision"] == "PASS" and run["has_report"] == 1
    assert [(c["case_name"], c["passes"], c["n"], c["errors"]) for c in run["cases"]] == [("a", 5, 5, 0), ("b", 3, 5, 1)]
    assert "report_html" not in run


def test_server_ignores_client_supplied_overall(client):
    run_id = client.post("/api/eval_runs", json=payload(overall=1.0), headers=AUTH).get_json()["run_id"]
    assert client.get(f"/api/eval_runs/{run_id}", headers=AUTH).get_json()["overall"] == pytest.approx(0.8)


def test_list_filters(client):
    proj = "filter-" + uuid.uuid4().hex[:8]  # the test DB persists between runs, so never reuse a project name
    client.post("/api/eval_runs", json=payload(project=proj, agent="x"), headers=AUTH)
    client.post("/api/eval_runs", json=payload(project=proj, agent="y"), headers=AUTH)
    rows = client.get(f"/api/eval_runs?project={proj}", headers=AUTH).get_json()
    assert {r["agent"] for r in rows} == {"x", "y"}
    assert [r["agent"] for r in client.get(f"/api/eval_runs?project={proj}&agent=y", headers=AUTH).get_json()] == ["y"]
    assert all("report_html" not in r for r in rows)


def test_report_served_with_csp(client):
    run_id = client.post("/api/eval_runs", json=payload(), headers=AUTH).get_json()["run_id"]
    r = client.get(f"/api/eval_runs/{run_id}/report", headers=AUTH)
    assert r.status_code == 200 and r.mimetype == "text/html" and b"<body>r</body>" in r.data
    assert "script" not in r.headers["Content-Security-Policy"] and "default-src 'none'" in r.headers["Content-Security-Policy"]
    nr = client.post("/api/eval_runs", json=payload(report_html=None), headers=AUTH).get_json()["run_id"]
    assert client.get(f"/api/eval_runs/{nr}/report", headers=AUTH).status_code == 404
    assert client.get("/api/eval_runs/doesnotexist", headers=AUTH).status_code == 404


@pytest.mark.parametrize("over", [
    {"project": ""}, {"agent": None}, {"config_hash": 5}, {"kind": "nope"}, {"decision": "MAYBE"}, {"cases": []},
    {"cases": [{"case": "a", "passes": 6, "n": 5}]}, {"cases": [{"case": "a", "passes": -1, "n": 5}]},
    {"cases": [{"case": "a", "passes": 1, "n": 0}]}, {"cases": [{"case": "a", "passes": True, "n": 5}]},
    {"cases": [{"case": "a", "passes": 1, "n": 5}, {"case": "a", "passes": 1, "n": 5}]},
    {"cases": [{"case": "a", "passes": 1, "n": 5, "errors": -1}]}, {"api_calls": -1}, {"cost_usd": "free"},
])
def test_validation_rejects_bad_payloads(client, over):
    before = get_conn().execute("select count(*) from eval_runs").fetchone()[0]
    r = client.post("/api/eval_runs", json=payload(**over), headers=AUTH)
    assert r.status_code == 400 and "error" in r.get_json()
    assert get_conn().execute("select count(*) from eval_runs").fetchone()[0] == before


def test_non_object_body_rejected(client):
    assert client.post("/api/eval_runs", data="[]", headers={**AUTH, "Content-Type": "application/json"}).status_code == 400
