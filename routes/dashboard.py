import csv
import io
import json
import os
from datetime import datetime, timezone, timedelta

PDT = timezone(timedelta(hours=-7))

import requests as http
from flask import Blueprint, render_template, jsonify, Response, request
from src.store import get_conn
from src.governance import _load_config

bp = Blueprint("dashboard", __name__)


def _summary():
    with get_conn() as conn:
        return dict(conn.execute("""
            SELECT
                COUNT(*)                          AS total_calls,
                ROUND(SUM(cost_usd), 4)           AS total_cost,
                ROUND(AVG(latency_ms), 0)         AS avg_latency,
                COUNT(DISTINCT project)            AS num_projects,
                COUNT(DISTINCT session_id)         AS num_sessions
            FROM llm_calls
        """).fetchone())


def _daily():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT date(ts, '-7 hours') AS day, COUNT(*) AS calls, ROUND(SUM(cost_usd), 4) AS cost
            FROM llm_calls
            GROUP BY date(ts, '-7 hours')
            ORDER BY day ASC
            LIMIT 14
        """).fetchall()
    return [dict(r) for r in rows]


def _by_model():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT model, COUNT(*) AS calls, ROUND(SUM(cost_usd), 4) AS cost,
                   ROUND(AVG(latency_ms), 0) AS avg_latency
            FROM llm_calls
            GROUP BY model
            ORDER BY calls DESC
        """).fetchall()
    return [dict(r) for r in rows]


def _governance_summary():
    with get_conn() as conn:
        return dict(conn.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN gov_flags != '[]' AND gov_flags IS NOT NULL AND reviewed_at IS NULL THEN 1 ELSE 0 END), 0) AS flagged_calls,
                COALESCE(SUM(CASE WHEN error LIKE '%governance%' AND reviewed_at IS NULL THEN 1 ELSE 0 END), 0) AS blocked_calls
            FROM llm_calls
        """).fetchone())


def _by_project():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                p.project,
                p.calls,
                p.sessions,
                p.total_cost,
                p.avg_latency,
                (SELECT model FROM llm_calls
                 WHERE project = p.project
                 GROUP BY model ORDER BY COUNT(*) DESC LIMIT 1) AS top_model
            FROM (
                SELECT project,
                       COUNT(*) AS calls,
                       COUNT(DISTINCT session_id) AS sessions,
                       ROUND(SUM(cost_usd), 4) AS total_cost,
                       ROUND(AVG(latency_ms), 0) AS avg_latency
                FROM llm_calls
                GROUP BY project
                ORDER BY total_cost DESC
            ) p
        """).fetchall()
    return [dict(r) for r in rows]


def _recent(project=None, session=None, limit=100, flagged=False):
    filters, params = [], []
    if project:
        filters.append("project = ?")
        params.append(project)
    if session:
        filters.append("session_id = ?")
        params.append(session)
    if flagged:
        filters.append("(gov_flags != '[]' AND gov_flags IS NOT NULL AND reviewed_at IS NULL)")
    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    with get_conn() as conn:
        rows = conn.execute(f"""
            SELECT id, datetime(ts, '-7 hours') AS ts, provider, model, session_id, project,
                   input_tokens, output_tokens, cost_usd, latency_ms,
                   gov_flags, error
            FROM llm_calls {where}
            ORDER BY ts DESC LIMIT ?
        """, params + [limit]).fetchall()
    return [dict(r) for r in rows]


def _session_calls(session_id):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, datetime(ts, '-7 hours') AS ts, provider, model, project,
                   input_tokens, output_tokens, cost_usd, latency_ms,
                   gov_flags, error, raw_prompt, raw_response, tool_calls
            FROM llm_calls
            WHERE session_id = ?
            ORDER BY ts ASC
        """, (session_id,)).fetchall()
    return [dict(r) for r in rows]


@bp.route("/session/<session_id>")
def session_view(session_id):
    calls = _session_calls(session_id)
    if not calls:
        return "Session not found", 404
    meta = {
        "session_id": session_id,
        "project": calls[0].get("project", ""),
        "first_ts": calls[0].get("ts", ""),
        "last_ts": calls[-1].get("ts", ""),
        "total_calls": len(calls),
        "total_input_tokens": sum(c.get("input_tokens") or 0 for c in calls),
        "total_output_tokens": sum(c.get("output_tokens") or 0 for c in calls),
        "total_cost": round(sum(c.get("cost_usd") or 0 for c in calls), 6),
    }
    return render_template("session.html", meta=meta, calls=calls)


@bp.route("/")
def index():
    project = request.args.get("project")
    try:
        limit = max(1, int(request.args.get("limit", 20)))
    except (ValueError, TypeError):
        limit = 20
    results = _gather_instances()
    sessions, trivial_count = _merge_recent_sessions(results, project=project, limit=limit)
    return render_template("index.html",
        summary=_merge_summary(results, project=project),
        recent_sessions=sessions,
        trivial_count=trivial_count,
        daily=_merge_daily(results),
        by_model=_merge_by_model(results),
        by_project=_merge_by_project(results),
        filter_project=project or "",
        limit=limit,
    )


@bp.route("/flags/mark-reviewed", methods=["POST"])
def mark_reviewed():
    with get_conn() as conn:
        conn.execute("""
            UPDATE llm_calls
            SET reviewed_at = datetime('now')
            WHERE (gov_flags != '[]' AND gov_flags IS NOT NULL)
               OR error LIKE '%governance%'
        """)
        conn.commit()
    return ("", 204)


@bp.route("/api/stats")
def stats():
    return jsonify({
        "summary": _summary(),
        "daily": _daily(),
        "by_model": _by_model(),
        "by_project": _by_project(),
        "recent": _recent(limit=500),
    })


def _gather_instances():
    children = _load_config().get("aggregator", {}).get("children", [])
    instance = os.getenv("AIGLUE_INSTANCE_NAME") or os.getenv("AIGLUE_DEFAULT_PROJECT", "unnamed")
    local_recent = _recent(limit=500)
    for row in local_recent:
        row["_source"] = instance
    results = [{
        "_source": instance,
        "_error": None,
        "summary": _summary(),
        "by_project": _by_project(),
        "by_model": _by_model(),
        "daily": _daily(),
        "gov": _governance_summary(),
        "recent": local_recent,
    }]
    for child in children:
        name = child.get("name", "unknown")
        url = child.get("url", "").rstrip("/")
        try:
            r = http.get(f"{url}/api/summary", timeout=5)
            r.raise_for_status()
            data = r.json()
            data["_source"] = name
            data["_error"] = None
            for row in (data.get("recent") or []):
                row["_source"] = name
        except Exception as e:
            data = {
                "_source": name, "_error": str(e),
                "summary": {}, "by_project": [], "by_model": [], "daily": [], "gov": {}, "recent": [],
            }
        results.append(data)
    return results


def _merge_summary(results, project=None):
    if project:
        by_project = _merge_by_project(results)
        proj = next((p for p in by_project if p["project"] == project), None)
        calls = [c for r in results for c in (r.get("recent") or []) if c.get("project") == project]
        latencies = [c["latency_ms"] for c in calls if c.get("latency_ms")]
        return {
            "total_calls":  proj["calls"] if proj else 0,
            "total_cost":   proj["total_cost"] if proj else 0.0,
            "num_sessions": proj["sessions"] if proj else 0,
            "num_projects": 1,
            "avg_latency":  round(sum(latencies) / len(latencies)) if latencies else 0,
        }
    all_projects = {p.get("project") for r in results for p in (r.get("by_project") or []) if p.get("project") != "claude-code"}
    all_calls = [c for r in results for c in (r.get("recent") or [])]
    latencies = [c["latency_ms"] for c in all_calls if c.get("latency_ms")]
    return {
        "total_calls":  sum((r.get("summary") or {}).get("total_calls") or 0 for r in results),
        "total_cost":   round(sum((r.get("summary") or {}).get("total_cost") or 0 for r in results), 4),
        "num_sessions": sum((r.get("summary") or {}).get("num_sessions") or 0 for r in results),
        "num_projects": len(all_projects),
        "avg_latency":  round(sum(latencies) / len(latencies)) if latencies else 0,
    }


def _merge_recent_sessions(results, project=None, limit=20):
    all_calls = [c for r in results for c in (r.get("recent") or [])]
    if project:
        all_calls = [c for c in all_calls if c.get("project") == project]
    sessions = {}
    for c in all_calls:
        sid = c.get("session_id") or "unknown"
        if sid not in sessions:
            sessions[sid] = {
                "session_id": sid,
                "project": c.get("project", ""),
                "_source": c.get("_source", ""),
                "first_ts": c.get("ts", ""),
                "last_ts": c.get("ts", ""),
                "calls": 0,
                "total_input": 0,
                "total_output": 0,
                "total_cost": 0.0,
                "model": c.get("model", ""),
                "has_flags": False,
            }
        s = sessions[sid]
        s["calls"] += 1
        s["total_input"] += c.get("input_tokens") or 0
        s["total_output"] += c.get("output_tokens") or 0
        s["total_cost"] = round(s["total_cost"] + (c.get("cost_usd") or 0), 6)
        ts = c.get("ts", "")
        if ts < s["first_ts"]:
            s["first_ts"] = ts
        if ts > s["last_ts"]:
            s["last_ts"] = ts
            s["model"] = c.get("model", s["model"])
        if c.get("gov_flags") and c["gov_flags"] != "[]":
            s["has_flags"] = True
    active = [s for s in sessions.values() if s["total_input"] + s["total_output"] > 0]
    for s in active:
        s["is_trivial"] = s["calls"] == 1 and s["total_output"] < 100
    real    = sorted([s for s in active if not s["is_trivial"]], key=lambda s: s["last_ts"], reverse=True)
    trivial = sorted([s for s in active if     s["is_trivial"]], key=lambda s: s["last_ts"], reverse=True)
    real_limited = real[:limit] if limit else real
    merged = sorted(real_limited + trivial, key=lambda s: s["last_ts"], reverse=True)
    return merged, len(trivial)


def _merge_by_model(results):
    merged = {}
    for r in results:
        for m in (r.get("by_model") or []):
            name = m.get("model", "unknown")
            if name not in merged:
                merged[name] = {"model": name, "calls": 0, "cost": 0.0, "_latency_sum": 0}
            calls = m.get("calls") or 0
            merged[name]["calls"] += calls
            merged[name]["cost"] = round(merged[name]["cost"] + (m.get("cost") or 0), 4)
            merged[name]["_latency_sum"] += calls * (m.get("avg_latency") or 0)
    out = []
    for m in merged.values():
        total = m["calls"]
        out.append({
            "model": m["model"],
            "calls": total,
            "cost": m["cost"],
            "avg_latency": round(m["_latency_sum"] / total) if total else 0,
        })
    return sorted(out, key=lambda x: x["calls"], reverse=True)


def _merge_by_project(results):
    merged = {}
    for r in results:
        for p in (r.get("by_project") or []):
            name = p.get("project", "unknown")
            if name not in merged:
                merged[name] = {
                    "project": name, "calls": 0, "sessions": 0, "total_cost": 0.0,
                    "_latency_sum": 0, "_model_votes": {},
                }
            calls = p.get("calls") or 0
            merged[name]["calls"] += calls
            merged[name]["sessions"] += p.get("sessions") or 0
            merged[name]["total_cost"] = round(merged[name]["total_cost"] + (p.get("total_cost") or 0), 4)
            merged[name]["_latency_sum"] += calls * (p.get("avg_latency") or 0)
            top_model = p.get("top_model")
            if top_model:
                mv = merged[name]["_model_votes"]
                mv[top_model] = mv.get(top_model, 0) + calls
    out = []
    for m in merged.values():
        total = m["calls"]
        out.append({
            "project": m["project"],
            "calls": total,
            "sessions": m["sessions"],
            "total_cost": m["total_cost"],
            "avg_latency": round(m["_latency_sum"] / total) if total else 0,
            "top_model": max(m["_model_votes"], key=m["_model_votes"].get) if m["_model_votes"] else "",
        })
    return sorted(out, key=lambda x: x["total_cost"], reverse=True)


def _merge_daily(results):
    merged = {}
    for r in results:
        for d in (r.get("daily") or []):
            day = d.get("day", "")
            if day not in merged:
                merged[day] = {"day": day, "calls": 0, "cost": 0.0}
            merged[day]["calls"] += d.get("calls") or 0
            merged[day]["cost"] = round(merged[day]["cost"] + (d.get("cost") or 0), 4)
    return sorted(merged.values(), key=lambda x: x["day"])


def _merge_gov(results):
    return {
        "flagged_calls": sum((r.get("gov") or {}).get("flagged_calls") or 0 for r in results),
        "blocked_calls": sum((r.get("gov") or {}).get("blocked_calls") or 0 for r in results),
    }


@bp.route("/api/summary")
def summary_api():
    instance = os.getenv("AIGLUE_INSTANCE_NAME") or os.getenv("AIGLUE_DEFAULT_PROJECT", "unnamed")
    return jsonify({
        "instance": instance,
        "as_of": datetime.now(PDT).isoformat(),
        "summary": _summary(),
        "by_project": _by_project(),
        "by_model": _by_model(),
        "daily": _daily(),
        "gov": _governance_summary(),
        "recent": _recent(limit=500),
    })


@bp.route("/aggregate")
def aggregate():
    results = _gather_instances()
    merged = {
        "total_calls":  sum((r.get("summary") or {}).get("total_calls") or 0 for r in results),
        "total_cost":   round(sum((r.get("summary") or {}).get("total_cost") or 0 for r in results), 4),
        "num_sessions": sum((r.get("summary") or {}).get("num_sessions") or 0 for r in results),
    }
    return render_template("aggregate.html",
        results=results,
        merged=merged,
        as_of=datetime.now(PDT).strftime("%Y-%m-%d %H:%M PDT"),
    )


@bp.route("/executive")
def executive():
    results = _gather_instances()
    return render_template("executive.html",
        summary=_merge_summary(results),
        by_project=_merge_by_project(results),
        daily=_merge_daily(results),
        gov=_merge_gov(results),
        as_of=datetime.now(PDT).strftime("%Y-%m-%d %H:%M PDT"),
    )


@bp.route("/export")
def export():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM llm_calls ORDER BY ts DESC").fetchall()
    out = io.StringIO()
    if rows:
        writer = csv.DictWriter(out, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows([dict(r) for r in rows])
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )
