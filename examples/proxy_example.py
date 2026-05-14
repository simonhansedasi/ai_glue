"""
Demonstrates the proxy mode — no code changes to the "existing app."
The only change is OPENAI_BASE_URL / ANTHROPIC_BASE_URL pointing at ai_glue.

Run ai_glue first:
    python app.py   (or it's already running on the Pi at :5010)

Then run this:
    python examples/proxy_example.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

# ── OpenAI via proxy ──────────────────────────────────────────────────────────
# The only change from a normal OpenAI app: base_url points at ai_glue.
# X-Aiglue-* headers are optional — they tag the call in the audit log.

import openai

oai = openai.OpenAI(
    api_key=os.getenv("OPENAI_API_KEY", "no-key-needed-for-demo"),
    base_url="http://localhost:5010/proxy/openai/v1",
    default_headers={
        "X-Aiglue-Session": "demo-proxy-session",
        "X-Aiglue-Project": "proxy-demo",
    },
)

print("=== OpenAI via ai_glue proxy ===")
print("(Set OPENAI_API_KEY in .env for a real response)")
try:
    resp = oai.chat.completions.create(
        model="gpt-4o-mini",
        max_tokens=64,
        messages=[{"role": "user", "content": "Name one benefit of AI governance in one sentence."}],
    )
    print(resp.choices[0].message.content)
except Exception as e:
    print(f"  (skipped: {e})")

# ── Anthropic via proxy ───────────────────────────────────────────────────────
import anthropic

ant = anthropic.Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY", "no-key-needed-for-demo"),
    base_url="http://localhost:5010/proxy/anthropic",
    default_headers={
        "X-Aiglue-Session": "demo-proxy-session",
        "X-Aiglue-Project": "proxy-demo",
    },
)

print("\n=== Anthropic via ai_glue proxy ===")
print("(Set ANTHROPIC_API_KEY in .env for a real response)")
try:
    resp = ant.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=64,
        messages=[{"role": "user", "content": "Name one benefit of AI governance in one sentence."}],
    )
    print(resp.content[0].text)
except Exception as e:
    print(f"  (skipped: {e})")

print("\nCheck http://localhost:5010 — both calls appear in the dashboard.")
print("The 'existing app' needed zero code changes. Just the base_url.")
