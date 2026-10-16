"""Central staging area: project tiles -> project (agents + subagent tree) -> agent (versions, evals, production).

All status logic lives in src/staging.py (pure, unit-tested). This module only gathers rows and renders.
Production rows come from every instance (children ride along in /api/summary as `agent_stats`); eval runs are the
ones posted to THIS instance (point each project's gate at the aggregator).
"""
from flask import Blueprint, abort, jsonify, render_template

from src import staging
from src.store import get_conn

bp = Blueprint("staging", __name__)


def _eval_runs():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT rowid AS seq, run_id, ts, project, agent, config_hash, kind, decision, overall, n_runs, "
            "report_html IS NOT NULL AS has_report FROM eval_runs ORDER BY ts, rowid").fetchall()
    return [dict(r) for r in rows]


def _model():
    from routes.dashboard import _gather_instances  # lazy: avoids an import cycle with the dashboard module
    rows, mix = [], []
    for result in _gather_instances():
        rows += [dict(r, _source=result.get("_source")) for r in (result.get("agent_stats") or [])]
        mix += result.get("tool_mix") or []
    return staging.build(rows, _eval_runs(), tool_mix=mix)


def _project(model, name):
    return next((p for p in model["projects"] if p["name"] == name), None) or abort(404)


@bp.route("/staging")
def home():
    return render_template("staging_home.html", projects=_model()["projects"])


@bp.route("/staging/<project>")
def project_page(project):
    p = _project(_model(), project)
    return render_template("staging_project.html", project=p, amap={a["name"]: a for a in p["agents"]})


@bp.route("/staging/<project>/<agent>")
def agent_page(project, agent):
    p = _project(_model(), project)
    a = next((x for x in p["agents"] if x["name"] == agent), None) or abort(404)
    return render_template("staging_agent.html", project=p, agent=a,
                           drift_limit=staging.DRIFT_TVD, min_calls=staging.MIN_DRIFT_CALLS)


@bp.route("/api/staging")
def api():
    return jsonify(_model())
