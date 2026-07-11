"""SQLite-backed chat session store.

Survives `--reload` and process crashes while keeping the single-machine
kiosk's footprint tiny. Schema:

    sessions(session_id TEXT PRIMARY KEY, last_used REAL, messages TEXT)

`messages` holds the JSON-encoded `AgentSession.messages` list — provider /
client / model are recreated from `Settings` on every request, so only the
mutable conversation state needs persistence.

WAL mode is enabled for safe concurrent reads while a single writer holds
the per-store lock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

_DEFAULT_DB_PATH = Path(".agent_state") / "sessions.db"
_DEFAULT_TTL_SECONDS = 1800.0


class ChatSessionStore:
    """Per-process SQLite store for chat session message logs."""

    def __init__(
        self,
        db_path: Path | str = _DEFAULT_DB_PATH,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._db_path = Path(db_path)
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so FastAPI thread-pool callers share the
        # connection; the lock serialises writes.
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                last_used  REAL NOT NULL,
                messages   TEXT NOT NULL
            )"""
        )

    def create(self) -> str:
        session_id = str(uuid.uuid4())
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(session_id, last_used, messages) VALUES (?, ?, ?)",
                (session_id, now, "[]"),
            )
        return session_id

    def load_messages(self, session_id: str) -> list[dict] | None:
        """Return messages and bump `last_used`, or None if missing / expired."""
        self.purge_expired()
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT last_used, messages FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            if now - row[0] > self._ttl:
                self._conn.execute(
                    "DELETE FROM sessions WHERE session_id = ?",
                    (session_id,),
                )
                return None
            self._conn.execute(
                "UPDATE sessions SET last_used = ? WHERE session_id = ?",
                (now, session_id),
            )
        return json.loads(row[1])

    def save_messages(self, session_id: str, messages: list[dict]) -> None:
        payload = json.dumps(messages, ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET messages = ?, last_used = ? WHERE session_id = ?",
                (payload, time.time(), session_id),
            )

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM sessions WHERE session_id = ?",
                (session_id,),
            )

    def purge_expired(self) -> list[str]:
        """Delete rows past TTL; return the purged session_ids.

        Callers with per-session in-memory state keyed by session_id (e.g.
        api.chat's `_session_locks`) use the return value to prune their own
        state in step — otherwise a session that expires without ever being
        revisited leaves its entry behind forever.
        """
        cutoff = time.time() - self._ttl
        with self._lock:
            rows = self._conn.execute(
                "SELECT session_id FROM sessions WHERE last_used < ?",
                (cutoff,),
            ).fetchall()
            if rows:
                self._conn.execute(
                    "DELETE FROM sessions WHERE last_used < ?",
                    (cutoff,),
                )
        return [row[0] for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
