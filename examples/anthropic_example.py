"""
Minimal Anthropic example. Requires ANTHROPIC_API_KEY in .env.

    cd /home/simonhans/coding/ai_glue
    cp .env.example .env   # fill in your key
    python examples/anthropic_example.py
    python app.py          # then visit http://localhost:5010
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from src import GluedClient

client = GluedClient(
    provider="anthropic",
    session_id="demo-user-001",
    project="demo",
)

print("Calling claude-sonnet-4-6 via GluedClient...")

response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=256,
    messages=[{
        "role": "user",
        "content": "In two sentences, what is enterprise AI governance and why do companies need it?",
    }],
)

print("\nResponse:")
print(response.content[0].text)
print("\nCall logged to audit.db. Run `python app.py` to view the dashboard.")
