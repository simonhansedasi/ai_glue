"""
Demonstrates all three governance features without making real API calls.
Use this to show clients what governance catches before their data leaves the building.

    python examples/governance_demo.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

os.environ.setdefault("AIGLUE_DB", "/tmp/aiglue_demo.db")

from dotenv import load_dotenv
load_dotenv()

from src.store import init_db
from src.governance import check

init_db()

print("=" * 60)
print("ai_glue governance demo")
print("=" * 60)

# 1. Blocked model
print("\n[1] Blocked model test")
try:
    check("openai", "gpt-3.5-turbo-instruct", "demo", "user-1", "Hello")
except ValueError as e:
    print(f"  BLOCKED: {e}")

# 2. PII detection — email
print("\n[2] PII detection — email address in prompt")
flags = check("anthropic", "claude-sonnet-4-6", "demo", "user-1",
              "Please write an email to john.doe@acmecorp.com about the Q2 results.")
print(f"  FLAGS: {flags}")

# 3. PII detection — SSN
print("\n[3] PII detection — SSN in prompt")
flags = check("anthropic", "claude-sonnet-4-6", "demo", "user-1",
              "The employee SSN is 123-45-6789. Summarize their file.")
print(f"  FLAGS: {flags}")

# 4. Clean call
print("\n[4] Clean call — no flags expected")
flags = check("anthropic", "claude-sonnet-4-6", "demo", "user-1",
              "Summarize the quarterly earnings report.")
print(f"  FLAGS: {flags} (clean)")

print("\nAll checks complete. Run `python app.py` to see the dashboard.")
