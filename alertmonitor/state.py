"""Persistent state (SQLite): cooldowns, last values, seen item ids.

Operations are tiny single-row statements, so they are called synchronously
from the event loop; WAL mode keeps them well under a millisecond.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SEEN_LIMIT_PER_KEY = 2000
COOLDOWN_MAX_AGE = 7 * 24 * 3600


class StateStore:
    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._writes = 0
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS cooldown (key TEXT PRIMARY KEY, ts REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS seen (
                    key TEXT NOT NULL, item_id TEXT NOT NULL, PRIMARY KEY (key, item_id));
                """
            )
        self.prune()

    # -- key/value -------------------------------------------------------
    def get_value(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_value(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # -- cooldown --------------------------------------------------------
    def cooldown_ok(self, key: str, seconds: float, now: float | None = None) -> bool:
        """Atomically check-and-set. True means the alert may be sent."""
        now = time.time() if now is None else now
        with self._lock:
            row = self._db.execute("SELECT ts FROM cooldown WHERE key=?", (key,)).fetchone()
            if row and now - row[0] < seconds:
                return False
            self._db.execute(
                "INSERT INTO cooldown(key, ts) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET ts=excluded.ts", (key, now))
            self._writes += 1
        if self._writes % 500 == 0:
            self.prune()
        return True

    # -- seen ids --------------------------------------------------------
    def seen_ids(self, key: str) -> set[str]:
        with self._lock:
            rows = self._db.execute("SELECT item_id FROM seen WHERE key=?", (key,)).fetchall()
        return {r[0] for r in rows}

    def add_seen(self, key: str, ids: list[str]) -> None:
        if not ids:
            return
        with self._lock:
            self._db.executemany("INSERT OR IGNORE INTO seen(key, item_id) VALUES(?, ?)",
                                 [(key, i) for i in ids])
            self._db.execute(
                "DELETE FROM seen WHERE key=? AND rowid NOT IN "
                "(SELECT rowid FROM seen WHERE key=? ORDER BY rowid DESC LIMIT ?)",
                (key, key, SEEN_LIMIT_PER_KEY))

    # -- maintenance -----------------------------------------------------
    def prune(self, max_age: float = COOLDOWN_MAX_AGE) -> None:
        with self._lock:
            self._db.execute("DELETE FROM cooldown WHERE ts < ?", (time.time() - max_age,))

    def close(self) -> None:
        with self._lock:
            self._db.close()
