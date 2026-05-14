# ai_glue — contributor reference

## Architecture

```
ai_glue/
├── app.py               Flask entry point (port 5010); load_dotenv MUST fire before route imports
├── governance.yaml      Rules: model allowlist, PII patterns, spend caps, rate limits, aggregator children
├── src/
│   ├── proxy.py         GluedClient — drop-in SDK wrapper (wrapper mode)
│   ├── governance.py    Rule enforcement; reads governance.yaml fresh on every call
│   ├── logger.py        Writes one audit row to SQLite per call
│   ├── store.py         DB init + migrations; get_conn()
│   └── costs.py         Token → USD cost table; prefix-matches versioned model IDs
├── routes/
│   ├── proxy.py         Reverse proxy blueprint: /proxy/openai/<path>, /proxy/anthropic/<path>
│   └── dashboard.py     Dashboard blueprint: /, /executive, /aggregate, /session/<id>, /export, /api/summary
├── templates/           Jinja2 templates (dark theme)
└── static/style.css     Dark theme CSS
```

## Two deployment modes

**Wrapper** — new apps; wrap the SDK directly:
```python
from src.proxy import GluedClient
client = GluedClient("anthropic", session_id="s1", project="my-app")
response = client.messages.create(model="claude-sonnet-4-6", max_tokens=1024, messages=[...])
```

**Proxy** — existing apps; one env var change, zero code changes:
```bash
ANTHROPIC_BASE_URL=http://localhost:5010/proxy/anthropic
OPENAI_BASE_URL=http://localhost:5010/proxy/openai/v1
```

Tag calls with headers (optional):
```
X-Aiglue-Session: user-123
X-Aiglue-Project: my-app
```

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env        # add your API keys
python app.py               # dashboard at http://localhost:5010
python examples/governance_demo.py  # works without API keys (mocked)
```

## Tests

```bash
pytest tests/ -v
```

23 tests, no API keys required. All LLM calls are mocked.

## DB schema

```sql
CREATE TABLE llm_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- UTC
    provider      TEXT,
    model         TEXT,
    session_id    TEXT,
    project       TEXT,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL,
    latency_ms    INTEGER,
    prompt_hash   TEXT,
    raw_prompt    TEXT,        -- null if AIGLUE_LOG_RAW=false
    raw_response  TEXT,        -- null if AIGLUE_LOG_RAW=false
    tool_calls    TEXT,        -- JSON array of {name, input}; Anthropic only
    gov_flags     TEXT,        -- JSON array of warning strings
    error         TEXT,
    reviewed_at   TEXT         -- null = unreviewed
)
```

`init_db()` is idempotent and auto-migrates existing databases via `PRAGMA table_info`.

## Adding a new provider

1. Add a branch in `GluedClient._init_client()` returning the provider's SDK client
2. Add a `_call_<provider>` method mirroring `_call_anthropic`
3. Add a proxy class + route in `routes/proxy.py`
4. Add the provider's models to `src/costs.py`

## Adding a governance rule

Rules live in `src/governance.check()`:
- **Hard block** — raise `ValueError`; call never reaches the provider
- **Soft flag** — append a string to `flags`; stored in `gov_flags`, visible in dashboard

PII patterns and model allowlists live in `governance.yaml` — no code change needed for those.

## Key gotcha

`load_dotenv()` must be the first line of `app.py`, before any route imports. Routes read `os.getenv()` at request time, but if `load_dotenv()` runs after imports it may miss values in some environments.

## Multi-instance aggregation

Each instance exposes `/api/summary`. A parent instance can aggregate children by listing them in `governance.yaml`:

```yaml
aggregator:
  children:
    - name: prod
      url: http://your-prod-host:5010
```

The parent's dashboard merges data from all children automatically.

## Environment variables

| Var | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for Anthropic calls |
| `OPENAI_API_KEY` | — | Required for OpenAI calls |
| `AIGLUE_DB` | `audit.db` | Override DB path (useful for testing) |
| `AIGLUE_LOG_RAW` | `true` | Set `false` to store only prompt hash |
| `AIGLUE_DEFAULT_PROJECT` | `proxy` | Label for untagged calls |
| `AIGLUE_DEFAULT_SESSION` | `proxy` | Label for untagged sessions |
| `AIGLUE_INSTANCE_NAME` | `(DEFAULT_PROJECT)` | Name shown in parent aggregator |
