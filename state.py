"""SQLite-backed tracking of awards we've already processed.

Prevents re-alerting on the same contract across runs.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from config import CONFIG


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(CONFIG.state_db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_awards (
            award_id TEXT PRIMARY KEY,
            seen_at  TEXT NOT NULL
        )
    """)
    return conn


def is_seen(award_id: str) -> bool:
    with _conn() as c:
        cur = c.execute("SELECT 1 FROM seen_awards WHERE award_id = ?", (award_id,))
        return cur.fetchone() is not None


def mark_seen(award_id: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO seen_awards (award_id, seen_at) VALUES (?, ?)",
            (award_id, datetime.now(timezone.utc).isoformat()),
        )
