"""SQLite-backed chat session store.

Survives ``--reload`` and process crashes while keeping the single-machine
kiosk's footprint tiny. Schema:

    sessions(session_id TEXT PRIMARY KEY, last_used REAL, messages TEXT)
    session_tombstones(session_id TEXT PRIMARY KEY, deleted_at REAL)

``messages`` holds the JSON-encoded ``AgentSession.messages`` list — provider /
client / model are recreated from ``Settings`` on every request, so only the
mutable conversation state needs persistence.

Session IDs are client-owned. Explicit deletion writes a durable tombstone so
a delayed idempotent create request cannot resurrect a session the client has
already ended. Tombstones expire with the session TTL and are purged alongside
expired sessions.

WAL mode is enabled for safe concurrent reads. Multi-statement lifecycle
transitions use ``BEGIN IMMEDIATE`` so separate store connections cannot race a
stale read against a delete/recreate write.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Self

_DEFAULT_DB_PATH = Path(".agent_state") / "sessions.db"
_DEFAULT_TTL_SECONDS = 1800.0


class SessionTombstonedError(RuntimeError):
    """The client explicitly ended this session ID and it cannot be recreated."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"Chat session {session_id} was explicitly deleted")
        self.session_id = session_id


class ChatSessionStore:
    """SQLite store for client-owned chat session message logs."""

    def __init__(
        self,
        db_path: Path | str = _DEFAULT_DB_PATH,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._db_path = Path(db_path)
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None
        self._poisoned: BaseException | None = None
        self._opened = False
        self._closing = threading.Event()
        self._closed = False

    @property
    def opened(self) -> bool:
        """Whether this owner may currently serve database operations."""
        return self._opened and not self._closing.is_set() and not self._closed

    def open(self) -> Self:
        """Acquire and initialize the sqlite connection transactionally.

        The shell exists before acquisition, and the physical connection is
        adopted immediately after ``sqlite3.connect`` returns.  If schema setup
        fails, the exact connection remains owned but unusable until ``close``
        releases it; callers must never construct a replacement first.
        """
        with self._lock:
            if self.opened:
                return self
            if self._closing.is_set() or self._closed:
                raise RuntimeError("Chat session store is closed")
            if self._connection is not None:
                raise RuntimeError("Chat session store has unresolved initialization failure")

            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            # check_same_thread=False so FastAPI thread-pool callers share the
            # connection; the lock serialises all use of this connection.
            connection = sqlite3.connect(
                str(self._db_path),
                check_same_thread=False,
                isolation_level=None,
            )
            self._connection = connection
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    last_used  REAL NOT NULL,
                    messages   TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS session_tombstones (
                    session_id TEXT PRIMARY KEY,
                    deleted_at REAL NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS session_tombstones_deleted_at_idx "
                "ON session_tombstones(deleted_at)"
            )
            if self._closing.is_set():
                raise RuntimeError("Chat session store closed while opening")
            self._opened = True
        return self

    @property
    def _conn(self) -> sqlite3.Connection:
        """Expose the live connection internally and to focused store tests."""
        connection = self._connection
        if connection is None or not self.opened:
            raise RuntimeError("Chat session store is closed")
        return connection

    def _require_usable(self) -> sqlite3.Connection:
        connection = self._conn
        if self._poisoned is not None:
            raise RuntimeError(
                "Chat session store is unusable because transaction rollback failed"
            ) from self._poisoned
        return connection

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Own one cross-connection write transaction on the shared database."""
        connection = self._require_usable()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException as transaction_error:
            try:
                connection.rollback()
            except BaseException as rollback_error:
                failure = BaseExceptionGroup(
                    "Chat session transaction and rollback both failed",
                    [transaction_error, rollback_error],
                )
                # A connection with an unproven transaction boundary must never
                # serve another read or write. Keep owning it solely so close()
                # can release the underlying descriptor and retry if needed.
                self._poisoned = failure
                raise failure from None
            raise

    def create(self, session_id: str) -> None:
        """Idempotently create ``session_id`` without reviving explicit deletes.

        A live row is an exact no-op, including preserving both its messages and
        ``last_used`` timestamp. An expired row may be recreated under the same
        client-owned ID so a still-live voice connection can recover its durable
        conversation slot. A non-expired tombstone always wins and rejects the
        create, including when DELETE reached SQLite before a delayed PUT.
        """
        now = time.time()
        cutoff = now - self._ttl
        with self._lock, self._write_transaction() as conn:
            tombstone = conn.execute(
                "SELECT deleted_at FROM session_tombstones WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if tombstone is not None:
                if tombstone[0] >= cutoff:
                    raise SessionTombstonedError(session_id)
                conn.execute(
                    "DELETE FROM session_tombstones WHERE session_id = ?",
                    (session_id,),
                )

            session = conn.execute(
                "SELECT last_used FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if session is not None:
                if session[0] >= cutoff:
                    return
                conn.execute(
                    "DELETE FROM sessions WHERE session_id = ?",
                    (session_id,),
                )

            conn.execute(
                "INSERT INTO sessions(session_id, last_used, messages) VALUES (?, ?, ?)",
                (session_id, now, "[]"),
            )

    def load_messages(self, session_id: str) -> list[dict] | None:
        """Return messages and bump ``last_used``, or ``None`` if gone.

        Only this row's TTL is checked; the background purge loop sweeps the
        table. The read, expiry delete, and live-row timestamp update share one
        immediate transaction so another connection cannot recreate the same ID
        between the stale read and delete.
        """
        now = time.time()
        with self._lock, self._write_transaction() as conn:
            row = conn.execute(
                "SELECT last_used, messages FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            if now - row[0] > self._ttl:
                conn.execute(
                    "DELETE FROM sessions WHERE session_id = ?",
                    (session_id,),
                )
                return None
            conn.execute(
                "UPDATE sessions SET last_used = ? WHERE session_id = ?",
                (now, session_id),
            )
        return json.loads(row[1])

    def exists(self, session_id: str) -> bool:
        """Read-only presence check with the same TTL verdict as ``load_messages``.

        This pre-stream check deliberately neither bumps ``last_used`` nor
        deletes expired rows. The authoritative read still happens inside the
        session lock via ``load_messages``.
        """
        now = time.time()
        with self._lock:
            row = self._require_usable().execute(
                "SELECT last_used FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row is not None and now - row[0] <= self._ttl

    def save_messages(self, session_id: str, messages: list[dict]) -> None:
        payload = json.dumps(messages, ensure_ascii=False)
        with self._lock:
            self._require_usable().execute(
                "UPDATE sessions SET messages = ?, last_used = ? WHERE session_id = ?",
                (payload, time.time(), session_id),
            )

    def delete(self, session_id: str) -> None:
        """Delete the row and tombstone the ID in one durable transaction.

        Tombstoning an unknown ID is intentional: DELETE may arrive before a
        slow in-flight PUT for that same client-owned ID. Repeated DELETE calls
        refresh the tombstone lifetime and remain idempotent from the API's view.
        """
        now = time.time()
        with self._lock, self._write_transaction() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            conn.execute(
                """INSERT INTO session_tombstones(session_id, deleted_at)
                   VALUES (?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET deleted_at = excluded.deleted_at""",
                (session_id, now),
            )

    def purge_expired(self) -> list[str]:
        """Delete expired rows/tombstones and return purged live session IDs."""
        cutoff = time.time() - self._ttl
        with self._lock, self._write_transaction() as conn:
            rows = conn.execute(
                "SELECT session_id FROM sessions WHERE last_used < ?",
                (cutoff,),
            ).fetchall()
            conn.execute(
                "DELETE FROM sessions WHERE last_used < ?",
                (cutoff,),
            )
            conn.execute(
                "DELETE FROM session_tombstones WHERE deleted_at < ?",
                (cutoff,),
            )
        return [row[0] for row in rows]

    def session_ids(self) -> set[str]:
        """Return every session ID still represented by a live-row record."""
        with self._lock:
            rows = self._require_usable().execute("SELECT session_id FROM sessions").fetchall()
        return {row[0] for row in rows}

    def close(self) -> None:
        # Permanently reject new work before waiting for an in-flight operation
        # to release the serialization lock. A failed physical close retains the
        # exact connection and this terminal gate for a later retry.
        self._closing.set()
        with self._lock:
            connection = self._connection
            if connection is None:
                self._opened = False
                self._closed = True
                return
            connection.close()
            # Release identity only after sqlite confirms the physical close.
            # A close failure retains the exact connection for a later retry.
            self._connection = None
            self._opened = False
            self._closed = True
            self._poisoned = None
