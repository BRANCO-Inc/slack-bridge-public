"""SQLite-backed session storage with durable turn queues."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from bridge_logging import get_logger
from bridge_state import BridgeState, utc_now
from config import STATE_DB_PATH
from session import ACTIVE_VALUES, ALLOWED_TRANSITIONS, Session, Status, coerce_status

logger = get_logger(__name__)


class SessionStore:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or STATE_DB_PATH
        self.state = BridgeState(self.db_path)

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _pending_event_queue(self, channel_id: str, thread_ts: str) -> list[dict | str]:
        return self.state.list_visible_event_queue(channel_id=channel_id, thread_ts=thread_ts)

    def _attach_queue(self, session: Session) -> Session:
        session.event_queue = self._pending_event_queue(session.channel_id, session.thread_ts)
        return session

    def _row_to_session(self, row: sqlite3.Row | None) -> Session | None:
        if row is None:
            return None
        return self._attach_queue(Session.from_record(dict(row)))

    def _coerce_session(self, session: Session | dict) -> Session:
        if isinstance(session, Session):
            return session
        if hasattr(session, "to_record"):
            return Session.from_record(session.to_record())
        return Session.from_record(session)

    def _coerce_expected(self, expected_status: Status | str | set) -> set[Status]:
        if isinstance(expected_status, str) or hasattr(expected_status, "value"):
            expected_status = {expected_status}
        return {coerce_status(status) for status in expected_status}

    def _lookup_row(
        self, conn: sqlite3.Connection, key: str, thread_ts: str | None = None
    ) -> sqlite3.Row | None:
        if thread_ts is not None:
            return conn.execute(
                "SELECT * FROM sessions WHERE channel_id = ? AND thread_ts = ?",
                (key, thread_ts),
            ).fetchone()
        if key.count(":") >= 2:
            parts = key.split(":", 2)
            return conn.execute(
                "SELECT * FROM sessions WHERE channel_id = ? AND thread_ts = ?",
                (parts[1], parts[2]),
            ).fetchone()
        for field in ("worker_session_id", "case_id"):
            row = conn.execute(
                f"SELECT * FROM sessions WHERE {field} = ? ORDER BY created_at DESC LIMIT 1",
                (key,),
            ).fetchone()
            if row is not None:
                return row
        return None

    def _persist(self, conn: sqlite3.Connection, session: Session) -> None:
        record = session.to_db_record()
        columns = tuple(record.keys())
        values = tuple(record[column] for column in columns)
        assignments = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in {"channel_id", "thread_ts"}
        )
        conn.execute(
            f"""
            INSERT INTO sessions ({", ".join(columns)})
            VALUES ({", ".join("?" for _ in columns)})
            ON CONFLICT(channel_id, thread_ts) DO UPDATE SET
            {assignments}
            """,
            values,
        )

    def load(self, key: str, thread_ts: str | None = None) -> Session | None:
        with self._connect() as conn:
            row = self._lookup_row(conn, key, thread_ts)
        return self._row_to_session(row)

    def save(self, session: Session | dict) -> None:
        session = self._coerce_session(session)
        with self._connect(write=True) as conn:
            is_new_session = self._lookup_row(conn, session.channel_id, session.thread_ts) is None
            self._persist(conn, session)
        if is_new_session and session.event_queue:
            self.state.replace_event_queue(
                list(session.event_queue),
                conversation_identity=session.conversation_identity,
                channel_id=session.channel_id,
                thread_ts=session.thread_ts,
            )

    def create_if_absent(self, session: Session | dict) -> bool:
        session = self._coerce_session(session)
        record = session.to_db_record()
        columns = tuple(record.keys())
        values = tuple(record[column] for column in columns)
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                f"""
                INSERT OR IGNORE INTO sessions ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                """,
                values,
            )
            created = cursor.rowcount == 1
        if created and session.event_queue:
            self.state.replace_event_queue(
                list(session.event_queue),
                conversation_identity=session.conversation_identity,
                channel_id=session.channel_id,
                thread_ts=session.thread_ts,
            )
        return created

    def create_initial_queue_if_absent(
        self,
        session: Session | dict,
        payload: dict | str,
        *,
        lease_owner: str,
        lease_seconds: int = 300,
    ) -> int | None:
        """Persist a new session and its first claimed event as one transaction."""
        session = self._coerce_session(session)
        record = session.to_db_record()
        columns = tuple(record.keys())
        values = tuple(record[column] for column in columns)
        payload_json = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        event_id = payload.get("event_id") if isinstance(payload, dict) else None
        now = utc_now()
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                f"""
                INSERT OR IGNORE INTO sessions ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                """,
                values,
            )
            if cursor.rowcount != 1:
                return None
            queue_cursor = conn.execute(
                """
                INSERT INTO event_queue (
                    event_id, conversation_identity, channel_id, thread_ts, payload_json,
                    status, lease_owner, lease_expires_at, retry_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'dispatching', ?, datetime('now', ?), 1, ?, ?)
                """,
                (
                    event_id,
                    session.conversation_identity,
                    session.channel_id,
                    session.thread_ts,
                    payload_json,
                    lease_owner,
                    f"+{lease_seconds} seconds",
                    now,
                    now,
                ),
            )
            if event_id:
                conn.execute(
                    """
                    UPDATE event_ledger
                    SET status = 'dispatching', conversation_identity = ?,
                        last_updated_at = ?, failure_reason = NULL
                    WHERE event_id = ? AND status NOT IN ('done', 'failed')
                    """,
                    (session.conversation_identity, now, event_id),
                )
            return int(queue_cursor.lastrowid)

    def append_to_queue(self, key: str, item: dict | str) -> Session | None:
        with self._connect() as conn:
            row = self._lookup_row(conn, key)
        if row is None:
            return None
        session = Session.from_record(dict(row))
        self.state.append_event_queue(
            item,
            conversation_identity=session.conversation_identity,
            channel_id=session.channel_id,
            thread_ts=session.thread_ts,
        )
        return self._attach_queue(session)

    def update_fields(self, key: str, updates: dict) -> Session | None:
        session = self.load(key)
        if session is None:
            return None
        for update_key, value in updates.items():
            setattr(session, update_key, value)
        queue_updated = "event_queue" in updates
        with self._connect(write=True) as conn:
            self._persist(conn, session)
        if queue_updated:
            self.state.replace_event_queue(
                list(session.event_queue),
                conversation_identity=session.conversation_identity,
                channel_id=session.channel_id,
                thread_ts=session.thread_ts,
            )
        return session

    def update_if_status(
        self, key: str, expected_status: Status | str | set, updates: dict
    ) -> Session | None:
        expected_statuses = self._coerce_expected(expected_status)
        with self._connect(write=True) as conn:
            row = self._lookup_row(conn, key)
            if row is None:
                return None
            session = self._row_to_session(row)
            if session is None or session.status not in expected_statuses:
                return None
            for update_key, value in updates.items():
                setattr(session, update_key, value)
            self._persist(conn, session)
        return session

    def transition(
        self,
        key: str,
        expected_status: Status | str | set,
        new_status: Status | str,
        updates: dict | None = None,
    ) -> Session | None:
        expected_statuses = self._coerce_expected(expected_status)
        target = coerce_status(new_status)
        with self._connect(write=True) as conn:
            row = self._lookup_row(conn, key)
            if row is None:
                return None
            session = self._row_to_session(row)
            if session is None or session.status not in expected_statuses:
                return None
            if target not in ALLOWED_TRANSITIONS[session.status]:
                logger.error(
                    "SessionStore.transition invalid transition: %s -> %s (%s)",
                    session.status.value,
                    target.value,
                    session.conversation_identity or session.thread_ts,
                )
                return None
            session.status = target
            for update_key, value in (updates or {}).items():
                setattr(session, update_key, value)
            self._persist(conn, session)
        return session

    def claim_queue_head(
        self, key: str, expected_item: dict | str, *, lease_owner: str, lease_seconds: int = 300
    ) -> dict | None:
        with self._connect() as conn:
            row = self._lookup_row(conn, key)
        if row is None:
            return None
        session = Session.from_record(dict(row))
        return self.state.claim_event_queue_head(
            channel_id=session.channel_id,
            thread_ts=session.thread_ts,
            expected_payload=expected_item,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
        )

    def mark_queue_retryable(self, queue_id: int, *, last_error: str) -> bool:
        return self.state.mark_event_queue_retryable(queue_id, last_error=last_error)

    def fail_queue_item(self, queue_id: int, *, reason: str) -> bool:
        return self.state.fail_event_queue_row(queue_id, reason=reason)

    def consume_queue_item(self, queue_id: int) -> bool:
        return self.state.consume_event_queue_row(queue_id)

    def consume_queue_head(
        self, key: str, expected_item: dict | str | None = None
    ) -> Session | None:
        with self._connect() as conn:
            row = self._lookup_row(conn, key)
        if row is None:
            return None
        session = Session.from_record(dict(row))
        if self.state.consume_event_queue_head(
            channel_id=session.channel_id,
            thread_ts=session.thread_ts,
            expected_payload=expected_item,
        ):
            return self._attach_queue(session)
        return None

    def delete(self, key: str) -> bool:
        session = self.load(key)
        if session is None:
            return False
        with self._connect(write=True) as conn:
            conn.execute(
                "DELETE FROM turn_attempts WHERE channel_id = ? AND thread_ts = ?",
                (session.channel_id, session.thread_ts),
            )
            conn.execute(
                "DELETE FROM sessions WHERE channel_id = ? AND thread_ts = ?",
                (session.channel_id, session.thread_ts),
            )
        self.state.fail_event_queue_for_session(
            channel_id=session.channel_id,
            thread_ts=session.thread_ts,
            reason="session_deleted",
        )
        return True

    def find_by_field(self, field: str, value) -> Session | None:
        if hasattr(value, "value"):
            value = value.value
        if field == "conversation_identity":
            return self.load(value)
        if field in {"case_id", "worker_session_id"}:
            with self._connect() as conn:
                row = conn.execute(
                    f"SELECT * FROM sessions WHERE {field} = ? ORDER BY created_at DESC LIMIT 1",
                    (value,),
                ).fetchone()
            return self._row_to_session(row)
        return None

    def list_all(self) -> list[Session]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM sessions ORDER BY created_at ASC").fetchall()
        return [session for row in rows if (session := self._row_to_session(row)) is not None]

    def active_count(self) -> int:
        placeholders = ", ".join("?" for _ in ACTIVE_VALUES)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM sessions WHERE status IN ({placeholders})",
                tuple(ACTIVE_VALUES),
            ).fetchone()
        return int(row[0]) if row is not None else 0
