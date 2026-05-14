# ai_glue

A lightweight Python library that wraps LLM provider clients, logs every call to a local audit database, enforces governance rules, and surfaces everything in a multi-view dashboard.

**The problem**: Companies adopt AI tools fast and without oversight. No audit trail, no cost visibility, no PII controls, no cross-environment picture.

**What this does**: Drop `GluedClient` into any existing app — or point existing apps at the proxy. Every call gets logged — provider, model, tokens, cost, latency, session, project. Deploy one instance per environment; the parent instance aggregates all children into a single unified view.

## Quickstart

```bash
cd ai_glue
pip install -r requirements.txt
cp .env.example .env       # add your API keys
python examples/governance_demo.py   # no keys needed — shows governance in action
python examples/anthropic_example.py
python app.py              # visit http://localhost:5010
```

## Two modes

### Mode 1 — Proxy (zero code changes in existing apps)

Point existing apps at ai_glue instead of the real provider. One env var change per app:

```bash
# Anthropic apps
ANTHROPIC_BASE_URL=http://your-host:5010/proxy/anthropic

# OpenAI apps
OPENAI_BASE_URL=http://your-host:5010/proxy/openai/v1
```

Tag calls with optional headers for project/session labeling:
```
X-Aiglue-Project: hr-bot
X-Aiglue-Session: user-123
```

Every call is intercepted, logged, and governance-checked. The app receives the real response unchanged.

If no headers are present, calls are labeled with `AIGLUE_DEFAULT_PROJECT` and `AIGLUE_DEFAULT_SESSION`
from the server's `.env` (defaults to `"proxy"`).

### Mode 2 — Wrapper (new apps, or when you control the code)

```python
from src import GluedClient

# Anthropic — same API as anthropic.Anthropic()
client = GluedClient("anthropic", session_id="user-123", project="hr-bot")
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=512,
    messages=[{"role": "user", "content": "..."}],
)

# OpenAI — same API as openai.OpenAI()
client = GluedClient("openai", session_id="user-123", project="support-bot")
response = client.chat.completions.create(
    model="gpt-4o",
    max_tokens=512,
    messages=[{"role": "user", "content": "..."}],
)
```

## Governance rules

Edit `governance.yaml` to configure:

- **model_allowlist** — hard-blocks calls to unapproved models (returns 403)
- **pii_detection** — flags prompts containing emails, SSNs, phone numbers, credit cards
- **pii_allowlist** — exact strings that should never trigger a PII flag (e.g. your own email in every system prompt)
- **daily_cost_cap_usd** — hard-blocks a project once its daily spend hits the cap
- **rate_limit_per_hour** — hard-blocks a session that exceeds call frequency

Hard-blocking governance violations are never logged (the call was never made). PII flags are logged as warnings and do not block calls.

## Dashboard — three views

All three views draw from the same unified dataset merged across all instances.

### `/` — Audit (engineers / team leads)

Full technical log. Every call from every instance in one table with an Instance column. Summary cards (total calls, cost, avg latency, projects, sessions) and charts (calls/day, cost/day) are also merged. Filters: project, session, or flagged-only. `?project=X` activates a "Project Report: X" header — useful as a shareable per-project artifact.

### `/executive` — Executive view (leadership)

Non-technical. No session IDs, no model names, no raw call table. Cards: Total AI Spend, Projects Active, Conversations, Risk Flags. Charts: Spend by Project (ranked horizontal bar), Daily Spend trend. If unreviewed PII flags or governance blocks exist, a risk callout appears with:
- "View flagged calls →" — links to the audit log filtered to flagged calls
- "Mark all reviewed" — acknowledges and clears the warning

Risk Flags count only shows **unreviewed** flags.

### `/aggregate` — Per-instance breakdown

One panel per instance showing its individual summary, by-project table, and by-model table. Useful for comparing environment-level activity (e.g. dev vs prod). Merged top-line totals shown at the top.

## Multi-instance aggregation

Run one ai_glue instance per environment. Each instance exposes `/api/summary`. A parent instance pulls from child URLs configured in `governance.yaml` and merges the data into a single unified view.

```
Instance A (claude-code) ──┐
Instance B (rippleforge)───┴──► Parent instance ──► unified /, /executive, /aggregate
```

To add a child — edit `governance.yaml` on the parent, no code changes:
```yaml
aggregator:
  children:
    - name: rippleforge-prod
      url: http://68.183.130.60:5010
```

Set `AIGLUE_INSTANCE_NAME` on each child so it appears with a meaningful label in parent views.

## Per-team API key mapping

Issue each team a fake key starting with its prefix in `governance.yaml`'s `teams:` section. Teams swap their API key for this one — zero other changes needed. The proxy detects the prefix, tags calls with the team's project name, and substitutes the real API key before forwarding.

```yaml
teams:
  sk-aiglue-eng:
    name: engineering
  sk-aiglue-marketing:
    name: marketing
```

## What gets logged

| Field | Description |
|---|---|
| ts | UTC timestamp (displayed as PDT in dashboard) |
| provider | anthropic / openai |
| model | exact model string |
| session_id | caller-supplied or auto-detected or AIGLUE_DEFAULT_SESSION |
| project | caller-supplied or auto-detected or AIGLUE_DEFAULT_PROJECT |
| input_tokens | from API response |
| output_tokens | from API response |
| cost_usd | estimated from token counts (6 decimal places) |
| latency_ms | wall time |
| prompt_hash | MD5 of prompt (first 16 chars) |
| raw_prompt | full text (if AIGLUE_LOG_RAW=true) |
| raw_response | full text (if AIGLUE_LOG_RAW=true) |
| gov_flags | JSON list of warnings triggered |
| error | exception message if call failed |
| reviewed_at | timestamp when flags were acknowledged via "Mark all reviewed" |

Set `AIGLUE_LOG_RAW=false` in `.env` to store only the hash when prompts themselves are sensitive.

## Auto-detection (Claude Code / proxy mode)

When running as a proxy for Claude Code, project and session can be inferred automatically — no headers needed:

- **Project**: scanned from `tool_use` file paths in the conversation (e.g. `/coding/rippleforge/` → `rippleforge`). Falls back to `AIGLUE_DEFAULT_PROJECT`.
- **Session**: MD5 hash of the first user message, prefixed `conv-`. Groups all turns of a conversation under the same session ID even though each API call is independent.

## Tests

```bash
pytest tests/ -v
```

23 tests. No API keys required. All LLM calls are mocked.

## Stack

- Python, Flask, SQLite
- Chart.js (CDN)
- Anthropic SDK, OpenAI SDK

## Environment variables

| Variable | Default | Description |
|---|---|---|
| ANTHROPIC_API_KEY | — | Required for Anthropic calls (GluedClient mode) |
| OPENAI_API_KEY | — | Required for OpenAI calls (GluedClient mode) |
| AIGLUE_DB | audit.db | Path to SQLite audit database |
| AIGLUE_LOG_RAW | true | Store full prompt/response text |
| AIGLUE_DEFAULT_PROJECT | proxy | Project label for untagged proxy calls |
| AIGLUE_DEFAULT_SESSION | proxy | Session label for untagged proxy calls |
| AIGLUE_INSTANCE_NAME | (AIGLUE_DEFAULT_PROJECT) | Label shown in parent aggregator views |
