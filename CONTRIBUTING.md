# Contributing to ai_glue

## Setup

```bash
git clone https://github.com/simonhansedasi/ai_glue.git
cd ai_glue
pip install -r requirements.txt
cp .env.example .env
cp governance.yaml.example governance.yaml
python app.py   # http://localhost:5010
```

## Tests

```bash
pytest tests/ -v
```

No API keys required — all LLM calls are mocked. Tests must pass before submitting a PR.

## Adding a provider

1. Add a branch in `GluedClient._init_client()` in `src/proxy.py` that returns the provider's SDK client
2. Add a `_call_<provider>()` method mirroring `_call_anthropic()`
3. Add a proxy class and route in `routes/proxy.py`
4. Add the provider's models and pricing to `src/costs.py`

## Adding a governance rule

Rules live in `src/governance.check()`. Two types:

- **Hard-block** — raise `ValueError`; the call is never forwarded and never logged
- **Soft-flag** — append a string to `flags`; the call proceeds and the flag is stored in `gov_flags`

## Submitting a PR

Fork the repo, work on a branch, and open a PR against `main`. Include a brief description of what the change does and why. If you're adding a feature, a test covering the new behavior is appreciated.
