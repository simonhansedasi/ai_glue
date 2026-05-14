import sqlite3
import os
from pathlib import Path


def _db_path():
    return os.getenv("AIGLUE_DB", str(Path(__file__).parent.parent / "audit.db"))


def get_conn():
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_calls (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                provider      TEXT,
                model         TEXT,
                session_id    TEXT,
                project       TEXT,
                input_tokens  INTEGER,
                output_tokens INTEGER,
                cost_usd      REAL,
                latency_ms    INTEGER,
                prompt_hash   TEXT,
                raw_prompt    TEXT,
                raw_response  TEXT,
                gov_flags     TEXT,
                error         TEXT,
                reviewed_at   TEXT
            )
        """)
        existing = [r[1] for r in conn.execute("PRAGMA table_info(llm_calls)").fetchall()]
        if "reviewed_at" not in existing:
            conn.execute("ALTER TABLE llm_calls ADD COLUMN reviewed_at TEXT")
        if "tool_calls" not in existing:
            conn.execute("ALTER TABLE llm_calls ADD COLUMN tool_calls TEXT")
        conn.commit()
