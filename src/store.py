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
        # Dashboard queries filter/sort/group on these columns on every page load;
        # without indexes each one was a full table scan. Indexes are on the small
        # columns only, so lookups avoid touching the (much larger) raw_prompt/
        # raw_response overflow pages entirely.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_project ON llm_calls(project)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_session ON llm_calls(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_model ON llm_calls(model)")
        conn.commit()
