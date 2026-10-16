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
        if "config_hash" not in existing:
            conn.execute("ALTER TABLE llm_calls ADD COLUMN config_hash TEXT")
        for col in ("agent", "parent_agent"):
            if col not in existing:
                conn.execute(f"ALTER TABLE llm_calls ADD COLUMN {col} TEXT")
        for col in ("cache_read_tokens", "cache_write_tokens"):  # billed at different rates than plain input
            if col not in existing:
                conn.execute(f"ALTER TABLE llm_calls ADD COLUMN {col} INTEGER")
        # Dashboard queries filter/sort/group on these columns on every page load;
        # without indexes each one was a full table scan. Indexes are on the small
        # columns only, so lookups avoid touching the (much larger) raw_prompt/
        # raw_response overflow pages entirely.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_project ON llm_calls(project)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_session ON llm_calls(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_model ON llm_calls(model)")
        # Eval results posted by each project's gate (see routes/evals.py). Additive, like the llm_calls migrations.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS eval_runs (
                run_id      TEXT PRIMARY KEY,
                ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                project     TEXT NOT NULL,
                agent       TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                kind        TEXT NOT NULL,
                decision    TEXT,
                overall     REAL,
                n_cases     INTEGER,
                n_runs      INTEGER,
                api_calls   INTEGER,
                tokens_in   INTEGER,
                tokens_out  INTEGER,
                cost_usd    REAL,
                report_html TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS eval_case_results (
                run_id    TEXT NOT NULL,
                case_name TEXT NOT NULL,
                passes    INTEGER NOT NULL,
                n         INTEGER NOT NULL,
                errors    INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (run_id, case_name)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_runs_agent ON eval_runs(project, agent, ts)")
        conn.commit()
