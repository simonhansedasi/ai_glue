"""
Minimal OpenAI example. Requires OPENAI_API_KEY in .env.

    cd /home/simonhans/coding/ai_glue
    cp .env.example .env   # fill in your key
    python examples/openai_example.py
    python app.py          # then visit http://localhost:5010
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from src import GluedClient

client = GluedClient(
    provider="openai",
    session_id="demo-user-001",
    project="demo",
)

print("Calling gpt-4o-mini via GluedClient...")

response = client.chat.completions.create(
    model="gpt-4o-mini",
    max_tokens=256,
    messages=[{
        "role": "user",
        "content": "In two sentences, what is enterprise AI governance and why do companies need it?",
    }],
)

print("\nResponse:")
print(response.choices[0].message.content)
print("\nCall logged to audit.db. Run `python app.py` to view the dashboard.")
