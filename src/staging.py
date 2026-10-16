"""Agent staging: health status per agent, subagent tree, per-project roll-up.

Pure functions over plain dicts (no database, no Flask) so every health rule has a unit test.

Inputs
  agent_rows: production traffic grouped by (project, agent, parent_agent, config_hash), one dict per group:
              calls, errors, calls_7d, errors_7d, cost_usd, avg_latency_ms, first_ts, last_ts
              (rows from several ai_glue instances are merged with merge_rows)
  eval_runs : dicts with run_id, ts, project, agent, config_hash, kind (baseline|gate|adhoc), decision, overall

Status per agent (see agent_staging_plan.md, "Health badge rules")
  RED       latest gate run BLOCKED or was INVALID; or production error rate > 5% over 7 days (>= 20 calls);
            or eval runs exist but no baseline was ever recorded
  AMBER     production is running a config_hash with no baseline or gate PASS on record; or the last eval run
            is older than 14 days; or the production tool-call mix of a live config differs from its staging
            (eval) mix by more than DRIFT_TVD (see drift())
  GREEN     none of the above
  UNTESTED  the agent has production calls but no eval runs at all (neutral in roll-ups)
Deliberate deviation from the plan: "no baseline" is RED only when the agent has been tested at all; an agent
nobody has written evals for is UNTESTED, so the home page is not a wall of red for agents that were never covered.

A parent's roll-up status is the worst of its own and its subagents' (UNTESTED counts as neutral).
"""
from datetime import datetime, timedelta

UNTESTED, GREEN, AMBER, RED = "UNTESTED", "GREEN", "AMBER", "RED"
_SEVERITY = {GREEN: 0, AMBER: 1, RED: 2}
ERROR_RATE_RED = 0.05
MIN_CALLS_FOR_RATE = 20
STALE_EVAL_DAYS = 14
UNLABELLED = "(unlabelled)"
DRIFT_TVD = 0.30       # lenient on purpose. Calibrated 2026-10-13 on the real chat_copilot eval mix: pure iid sampling noise
                       # exceeds 0.30 in ~0.1% of draws at 30 calls (p99 0.24) and never from 60 calls up. Calls inside one
                       # conversation are correlated, so real noise is larger; revisit once real production traffic exists.
MIN_DRIFT_CALLS = 30   # per side; below this the mix is too noisy to compare
MAX_DEPTH = 4


def parse_ts(s):
    """SQLite 'YYYY-MM-DD HH:MM:SS' (UTC) or ISO string -> naive datetime, or None."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def merge_rows(rows):
    """Merge rows for the same (project, agent, parent_agent, config_hash) coming from different instances."""
    merged = {}
    for r in rows:
        key = (r["project"], r["agent"], r.get("parent_agent") or "", r.get("config_hash") or "")
        m = merged.get(key)
        if m is None:
            merged[key] = {"project": key[0], "agent": key[1], "parent_agent": key[2], "config_hash": key[3],
                           "calls": 0, "errors": 0, "calls_7d": 0, "errors_7d": 0, "cost_usd": 0.0,
                           "_lat_sum": 0.0, "first_ts": None, "last_ts": None}
            m = merged[key]
        for k in ("calls", "errors", "calls_7d", "errors_7d"):
            m[k] += r.get(k) or 0
        m["cost_usd"] += r.get("cost_usd") or 0.0
        m["_lat_sum"] += (r.get("avg_latency_ms") or 0) * (r.get("calls") or 0)
        for k, pick in (("first_ts", min), ("last_ts", max)):
            if r.get(k):
                m[k] = r[k] if m[k] is None else pick(m[k], r[k])
    out = []
    for m in merged.values():
        m["avg_latency_ms"] = round(m.pop("_lat_sum") / m["calls"]) if m["calls"] else None
        m["cost_usd"] = round(m["cost_usd"], 4)
        out.append(m)
    return out


def _eval_order(e):
    """Chronological order; runs posted within the same second fall back to insertion order (`seq`)."""
    return (e["ts"], e.get("seq", 0))


def merge_mix(rows):
    """Merge tool_mix rows (project, agent, config_hash, kind, calls, counts) from several instances."""
    merged = {}
    for r in rows:
        key = (r["project"], r["agent"], r["config_hash"], r["kind"])
        m = merged.setdefault(key, {"calls": 0, "counts": {}})
        m["calls"] += r.get("calls") or 0
        for tool, n in (r.get("counts") or {}).items():
            m["counts"][tool] = m["counts"].get(tool, 0) + n
    return merged


def tvd(a, b):
    """Total variation distance between two count dicts: the share of behaviour that differs (0 same, 1 disjoint)."""
    ta, tb = sum(a.values()), sum(b.values())
    if not ta or not tb:
        return None
    return 0.5 * sum(abs(a.get(k, 0) / ta - b.get(k, 0) / tb) for k in set(a) | set(b))


def drift(config_hash, prod, staging):
    """Compare production and staging tool mixes for one config. prod/staging: {"calls", "counts"} or None."""
    out = {"config_hash": config_hash, "tvd": None, "verdict": "no_staging", "prod_n": 0, "eval_n": 0, "rows": []}
    if not prod or not staging:
        return out
    out["prod_n"], out["eval_n"] = prod["calls"], staging["calls"]
    out["tvd"] = tvd(prod["counts"], staging["counts"])
    if prod["calls"] < MIN_DRIFT_CALLS or staging["calls"] < MIN_DRIFT_CALLS or out["tvd"] is None:
        out["verdict"] = "insufficient"
    else:
        out["verdict"] = "drift" if out["tvd"] > DRIFT_TVD else "ok"
    tp, ts = sum(prod["counts"].values()), sum(staging["counts"].values())
    out["rows"] = sorted(({"tool": t, "prod": prod["counts"].get(t, 0) / tp, "staging": staging["counts"].get(t, 0) / ts}
                          for t in set(prod["counts"]) | set(staging["counts"])),
                         key=lambda r: -max(r["prod"], r["staging"]))
    return out


def vetted_hashes(evals):
    """Config hashes that have a baseline or a passing gate run."""
    return {e["config_hash"] for e in evals
            if e["kind"] == "baseline" or (e["kind"] == "gate" and e.get("decision") == "PASS")}


def assess(rows, evals, now, drifts=()):
    """-> (status, reasons) for one agent from its production rows and its eval runs."""
    evals = sorted(evals, key=_eval_order)
    calls_7d = sum(r["calls_7d"] for r in rows)
    errors_7d = sum(r["errors_7d"] for r in rows)
    red, amber = [], []

    gates = [e for e in evals if e["kind"] == "gate"]
    if gates and gates[-1].get("decision") in ("BLOCK", "INVALID"):
        g = gates[-1]
        red.append(f"latest gate run {g['decision']} (candidate {g['config_hash'][:8]}, {g['ts']} UTC)")
    if calls_7d >= MIN_CALLS_FOR_RATE and errors_7d / calls_7d > ERROR_RATE_RED:
        red.append(f"production error rate {errors_7d / calls_7d:.0%} over 7 days ({errors_7d} of {calls_7d} calls)")
    if evals and not any(e["kind"] == "baseline" for e in evals):
        red.append("eval runs exist but no baseline has been recorded")

    if evals:
        live = {r["config_hash"] for r in rows if r["calls_7d"] > 0 and r["config_hash"]}
        unvetted = sorted(live - vetted_hashes(evals))
        if unvetted:
            amber.append("production is running config " + ", ".join(h[:8] for h in unvetted)
                         + " with no baseline or gate PASS on record")
        for d in drifts:
            if d["verdict"] == "drift":
                amber.append(f"production tool-call mix differs from staging for config {d['config_hash'][:8]} "
                             f"(distance {d['tvd']:.2f}, limit {DRIFT_TVD})")
        last = parse_ts(evals[-1]["ts"])
        if last and now - last > timedelta(days=STALE_EVAL_DAYS):
            amber.append(f"last eval run is {(now - last).days} days old (limit {STALE_EVAL_DAYS})")

    if red:
        return RED, red + amber
    if not evals:
        return UNTESTED, ["no eval runs recorded for this agent"]
    return (AMBER, amber) if amber else (GREEN, [])


def _worst(statuses):
    ranked = [s for s in statuses if s in _SEVERITY]
    return max(ranked, key=_SEVERITY.get) if ranked else UNTESTED


def build(agent_rows, eval_runs, now=None, tool_mix=None):
    """-> {"projects": [project dicts]}; each project holds its agents, the subagent tree and a roll-up."""
    now = now or datetime.utcnow()
    agent_rows = merge_rows(agent_rows)
    mix = merge_mix(tool_mix or [])
    by_agent, evals_by_agent, unlabelled = {}, {}, {}
    parents_of, children_of, root_candidates = {}, {}, {}

    for r in agent_rows:
        if r["agent"] == UNLABELLED:
            u = unlabelled.setdefault(r["project"], {"calls": 0, "calls_7d": 0})
            u["calls"] += r["calls"]
            u["calls_7d"] += r["calls_7d"]
            continue
        key = (r["project"], r["agent"])
        by_agent.setdefault(key, []).append(r)
        if r["parent_agent"]:
            parents_of.setdefault(key, set()).add(r["parent_agent"])
            children_of.setdefault((r["project"], r["parent_agent"]), set()).add(r["agent"])
        else:
            root_candidates.setdefault(r["project"], set()).add(r["agent"])
    for e in eval_runs:
        evals_by_agent.setdefault((e["project"], e["agent"]), []).append(e)

    keys = set(by_agent) | set(evals_by_agent) | {(p, a) for (p, a) in children_of}
    agents = {}
    for key in keys:
        rows = by_agent.get(key, [])
        evals = evals_by_agent.get(key, [])
        drifts = [drift(h, mix.get((key[0], key[1], h, "prod")), mix.get((key[0], key[1], h, "eval")))
                  for h in sorted({r["config_hash"] for r in rows if r["calls_7d"] > 0 and r["config_hash"]})]
        status, reasons = assess(rows, evals, now, drifts)
        vetted = vetted_hashes(evals)
        versions = sorted(({"config_hash": r["config_hash"], "calls": r["calls"], "errors": r["errors"],
                            "calls_7d": r["calls_7d"], "first_ts": r["first_ts"], "last_ts": r["last_ts"],
                            "vetted": r["config_hash"] in vetted, "live": r["calls_7d"] > 0}
                           for r in rows if r["config_hash"]), key=lambda v: v["last_ts"] or "", reverse=True)
        agents[key] = {
            "project": key[0], "name": key[1], "status": status, "reasons": reasons,
            "parents": sorted(parents_of.get(key, [])), "children": sorted(children_of.get(key, [])),
            "calls": sum(r["calls"] for r in rows), "calls_7d": sum(r["calls_7d"] for r in rows),
            "errors_7d": sum(r["errors_7d"] for r in rows), "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
            "last_ts": max((r["last_ts"] for r in rows if r["last_ts"]), default=None),
            "drift": drifts, "versions": versions, "evals": sorted(evals, key=_eval_order, reverse=True),
        }

    def rollup(key, path=()):
        a = agents[key]
        kids = [(key[0], c) for c in a["children"] if (key[0], c) in agents and (key[0], c) not in path]
        child_results = [rollup(k, path + (key,)) for k in kids]
        status = _worst([a["status"]] + [s for s, _ in child_results])
        untested = sum(1 for k in kids if agents[k]["status"] == UNTESTED) + sum(n for _, n in child_results)
        return status, untested

    for key, a in agents.items():
        a["rollup"], a["untested_below"] = rollup(key)

    def tree(project, name, depth, path):
        key = (project, name)
        node = {"name": name, "status": agents[key]["status"], "rollup": agents[key]["rollup"], "depth": depth}
        node["children"] = [] if depth >= MAX_DEPTH else [
            tree(project, c, depth + 1, path + (name,)) for c in agents[key]["children"]
            if (project, c) in agents and c not in path]
        return node

    projects = {}
    for key in agents:
        projects.setdefault(key[0], []).append(key[1])
    out = []
    for project, names in projects.items():
        names.sort()
        # roots: agents seen making calls with no parent, or that nothing is known to call
        roots = [n for n in names if n in root_candidates.get(project, set()) or not agents[(project, n)]["parents"]]
        plist = [agents[(project, n)] for n in names]
        out.append({
            "name": project, "agents": plist, "tree": [tree(project, n, 0, ()) for n in roots],
            "status": _worst([a["rollup"] for a in plist]),
            "n_agents": len(plist), "n_untested": sum(1 for a in plist if a["status"] == UNTESTED),
            "calls_7d": sum(a["calls_7d"] for a in plist), "errors_7d": sum(a["errors_7d"] for a in plist),
            "unlabelled": unlabelled.get(project, {"calls": 0, "calls_7d": 0}),
        })
    out.sort(key=lambda p: (-_SEVERITY.get(p["status"], -1), p["name"]))
    return {"projects": out}
