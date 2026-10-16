"""Eval results store: each project's gate posts its runs here; the dashboard reads them (see agent_staging_plan.md).

Writes require AIGLUE_TOKEN to be set on this instance (and, via app.require_token, the caller's credentials):
a new write endpoint must never be open, whatever the legacy default of the rest of the dashboard.
"""
import os
import uuid

from flask import Blueprint, Response, jsonify, request

from src.store import get_conn

bp = Blueprint("evals", __name__)

KINDS = {"baseline", "gate", "adhoc"}
DECISIONS = {"PASS", "BLOCK", "INVALID", None}
MAX_BODY = 8 * 1024 * 1024


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _validate(body):
    """Return an error string, or None if the payload is acceptable."""
    if not isinstance(body, dict):
        return "body must be a JSON object"
    for k in ("project", "agent", "config_hash"):
        v = body.get(k)
        if not isinstance(v, str) or not v.strip() or len(v) > 200:
            return f"{k} must be a non-empty string (max 200 chars)"
    if body.get("kind") not in KINDS:
        return f"kind must be one of {sorted(KINDS)}"
    if body.get("decision") not in DECISIONS:
        return "decision must be PASS, BLOCK, INVALID or null"
    cases = body.get("cases")
    if not isinstance(cases, list) or not cases:
        return "cases must be a non-empty list"
    seen = set()
    for c in cases:
        if not isinstance(c, dict) or not isinstance(c.get("case"), str) or not c["case"]:
            return "each case needs a non-empty string 'case'"
        if c["case"] in seen:
            return f"duplicate case {c['case']!r}"
        seen.add(c["case"])
        if not (_is_int(c.get("passes")) and _is_int(c.get("n")) and c["n"] >= 1 and 0 <= c["passes"] <= c["n"]):
            return f"case {c['case']!r}: need integers with 0 <= passes <= n and n >= 1"
        if not (_is_int(c.get("errors", 0)) and c.get("errors", 0) >= 0):
            return f"case {c['case']!r}: errors must be a non-negative integer"
    for k in ("api_calls", "tokens_in", "tokens_out"):
        if body.get(k) is not None and not (_is_int(body[k]) and body[k] >= 0):
            return f"{k} must be a non-negative integer"
    if body.get("cost_usd") is not None and not isinstance(body["cost_usd"], (int, float)):
        return "cost_usd must be a number"
    if body.get("report_html") is not None and not isinstance(body["report_html"], str):
        return "report_html must be a string"
    return None


@bp.route("/api/eval_runs", methods=["POST"])
def post_eval_run():
    if not os.getenv("AIGLUE_TOKEN"):
        return jsonify({"error": "eval_runs writes require AIGLUE_TOKEN to be set on this instance"}), 403
    if (request.content_length or 0) > MAX_BODY:
        return jsonify({"error": "payload too large"}), 413
    body = request.get_json(silent=True)
    err = _validate(body)
    if err:
        return jsonify({"error": err}), 400

    cases = body["cases"]
    total_passes = sum(c["passes"] for c in cases)
    total_n = sum(c["n"] for c in cases)
    run_id = uuid.uuid4().hex
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO eval_runs (run_id, project, agent, config_hash, kind, decision, overall, n_cases, n_runs,
                                      api_calls, tokens_in, tokens_out, cost_usd, report_html)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, body["project"], body["agent"], body["config_hash"], body["kind"], body.get("decision"),
             total_passes / total_n,  # computed here, not trusted from the client
             len(cases), total_n, body.get("api_calls"), body.get("tokens_in"), body.get("tokens_out"),
             body.get("cost_usd"), body.get("report_html")))
        conn.executemany(
            "INSERT INTO eval_case_results (run_id, case_name, passes, n, errors) VALUES (?, ?, ?, ?, ?)",
            [(run_id, c["case"], c["passes"], c["n"], c.get("errors", 0)) for c in cases])
        conn.commit()
    return jsonify({"run_id": run_id}), 201


@bp.route("/api/eval_runs")
def list_eval_runs():
    where, params = [], []
    for col in ("project", "agent", "config_hash", "kind"):
        if request.args.get(col):
            where.append(f"{col} = ?")
            params.append(request.args[col])
    limit = min(max(request.args.get("limit", 50, type=int), 1), 200)
    sql = ("SELECT run_id, ts, project, agent, config_hash, kind, decision, overall, n_cases, n_runs, api_calls, "
           "tokens_in, tokens_out, cost_usd, report_html IS NOT NULL AS has_report FROM eval_runs"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY ts DESC, rowid DESC LIMIT ?")
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params + [limit]).fetchall()]
    return jsonify(rows)


@bp.route("/api/eval_runs/<run_id>")
def get_eval_run(run_id):
    with get_conn() as conn:
        run = conn.execute(
            "SELECT run_id, ts, project, agent, config_hash, kind, decision, overall, n_cases, n_runs, api_calls, "
            "tokens_in, tokens_out, cost_usd, report_html IS NOT NULL AS has_report FROM eval_runs WHERE run_id = ?",
            (run_id,)).fetchone()
        if not run:
            return jsonify({"error": "not found"}), 404
        cases = conn.execute(
            "SELECT case_name, passes, n, errors FROM eval_case_results WHERE run_id = ? ORDER BY rowid", (run_id,)).fetchall()
    return jsonify({**dict(run), "cases": [dict(c) for c in cases]})


@bp.route("/api/eval_runs/<run_id>/report")
def get_eval_report(run_id):
    with get_conn() as conn:
        row = conn.execute("SELECT report_html FROM eval_runs WHERE run_id = ?", (run_id,)).fetchone()
    if not row or row["report_html"] is None:
        return jsonify({"error": "not found"}), 404
    # Stored HTML includes agent/user text (escaped by the generator); the CSP is defence in depth: no scripts, no network.
    return Response(row["report_html"], mimetype="text/html",
                    headers={"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
