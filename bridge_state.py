"""Slack Bridge durable ledgers backed by SQLite."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from runtime_env import LEGACY_TO_CURRENT_WINDOW_NAMES
from session import Status

EVENT_STATUSES = frozenset(
    {"received", "context_ready", "queued", "dispatching", "retryable", "done", "failed"}
)
CASE_REPLY_POSTABLE_SESSION_STATUSES = (Status.BUSY.value, Status.WAITING.value)
ACK_STATUSES = frozenset(
    {
        "pending",
        "posted",
        "suppressed_busy",
        "suppressed_external_shared",
        "suppressed_daily_limit",
        "suppressed_ratelimit",
        "timed_out",
        "failed",
    }
)
REPLY_STATUSES = frozenset({"received", "posted", "posted_unknown", "failed"})
TERMINAL_REPLY_ERRORS = frozenset(
    {
        "case_not_found",
        "discarded_cancelled",
        "invalid_case_reply_payload",
        "missing_session",
        "session_terminal",
        "stale_pane",
        "stale_session",
        "stale_turn",
        "stale_window",
    }
)
_UNSET = object()


def _default_db_path() -> str:
    from config import STATE_DB_PATH

    return STATE_DB_PATH


def _default_state_dir() -> str:
    from config import STATE_DIR

    return STATE_DIR


def _default_event_retention_days() -> int:
    from config import EVENT_LEDGER_RETENTION_DAYS

    return EVENT_LEDGER_RETENTION_DAYS


def _default_bootstrap_retention_hours() -> int:
    from config import BOOTSTRAP_LEDGER_RETENTION_HOURS

    return BOOTSTRAP_LEDGER_RETENTION_HOURS


def _default_reply_claim_timeout() -> int:
    from config import REPLY_CLAIM_TIMEOUT

    return REPLY_CLAIM_TIMEOUT


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(_json_dumps(payload).encode("utf-8")).hexdigest()


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _reply_failure_is_retryable(last_error: str | None) -> bool:
    if not last_error:
        return True
    return last_error not in TERMINAL_REPLY_ERRORS


@dataclass(slots=True)
class ClaimResult:
    claimed: bool
    status: str
    row: dict | None = None
    conflict: bool = False


class BridgeState:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or _default_db_path()
        state_dir = os.path.dirname(self.db_path) or _default_state_dir()
        os.makedirs(state_dir, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
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

    def _init_db(self) -> None:
        with self._connect(write=True) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS event_ledger (
                    event_id TEXT PRIMARY KEY,
                    envelope_id TEXT,
                    source_event_type TEXT NOT NULL,
                    conversation_identity TEXT,
                    status TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_updated_at TEXT NOT NULL,
                    failure_reason TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_event_ledger_last_updated_at
                ON event_ledger(last_updated_at);

                CREATE INDEX IF NOT EXISTS idx_event_ledger_conversation_identity
                ON event_ledger(conversation_identity);

                CREATE TABLE IF NOT EXISTS bootstrap_ledger (
                    bootstrap_key TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    source_event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_bootstrap_ledger_created_at
                ON bootstrap_ledger(created_at);

                CREATE TABLE IF NOT EXISTS reply_ledger (
                    reply_request_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    channel_id TEXT,
                    thread_ts TEXT,
                    reply_ts TEXT,
                    router_result_json TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_reply_ledger_case_id
                ON reply_ledger(case_id);

                CREATE INDEX IF NOT EXISTS idx_reply_ledger_updated_at
                ON reply_ledger(updated_at);

                CREATE TABLE IF NOT EXISTS ack_ledger (
                    event_id TEXT NOT NULL,
                    ack_kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reply_ts TEXT,
                    closed_by_reply_request_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (event_id, ack_kind)
                );

                CREATE INDEX IF NOT EXISTS idx_ack_ledger_updated_at
                ON ack_ledger(updated_at);

                CREATE TABLE IF NOT EXISTS sessions (
                    channel_id TEXT NOT NULL,
                    thread_ts TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    worker_session_id TEXT UNIQUE,
                    status TEXT NOT NULL DEFAULT 'creating',
                    failure_reason TEXT,
                    user_id TEXT,
                    call_name TEXT,
                    team_id TEXT,
                    channel_name TEXT,
                    channel_type TEXT,
                    is_external_shared INTEGER NOT NULL DEFAULT 0,
                    window_name TEXT,
                    pane_id TEXT,
                    active_turn_id TEXT,
                    active_turn_event_id TEXT,
                    active_turn_queue_id INTEGER,
                    active_turn_owner_user_id TEXT,
                    active_turn_started_at TEXT,
                    input_wait_json TEXT,
                    last_reply_request_id TEXT,
                    last_activity_at TEXT NOT NULL,
                    last_reply_ts TEXT,
                    last_substantive_reply_ts TEXT,
                    last_thread_message_ts TEXT,
                    last_event_id TEXT,
                    last_event_ts TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (channel_id, thread_ts)
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_status
                ON sessions(status);

                CREATE INDEX IF NOT EXISTS idx_sessions_case_id
                ON sessions(case_id);

                CREATE INDEX IF NOT EXISTS idx_sessions_last_activity_at
                ON sessions(last_activity_at);

                CREATE TABLE IF NOT EXISTS turn_attempts (
                    turn_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    thread_ts TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    owner_user_id TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    completion_action TEXT,
                    reply_accepted_count INTEGER NOT NULL DEFAULT 0,
                    discard_notified_at TEXT,
                    active_reply_request_id TEXT,
                    active_reply_claimed_at TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY (channel_id, thread_ts) REFERENCES sessions (channel_id, thread_ts)
                );

                CREATE INDEX IF NOT EXISTS idx_turn_attempts_session
                ON turn_attempts(channel_id, thread_ts, started_at);

                CREATE INDEX IF NOT EXISTS idx_turn_attempts_open
                ON turn_attempts(channel_id, thread_ts, started_at)
                WHERE completed_at IS NULL;

                CREATE INDEX IF NOT EXISTS idx_turn_attempts_prune
                ON turn_attempts(started_at);

                CREATE TABLE IF NOT EXISTS runtime_counters (
                    counter_name TEXT PRIMARY KEY,
                    counter_value INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_queue (
                    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE,
                    conversation_identity TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    thread_ts TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                """
            )
            self._migrate_session_worker_column(conn)
            self._migrate_session_window_names(conn)
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_event_queue_visible
                ON event_queue(channel_id, thread_ts, queue_id)
                WHERE status IN ('queued', 'retryable')
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_event_queue_status
                ON event_queue(status, lease_expires_at, queue_id)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_turn_attempts_active_reply
                ON turn_attempts(active_reply_request_id)
                WHERE active_reply_request_id IS NOT NULL
                """
            )

    def _migrate_session_worker_column(self, conn: sqlite3.Connection) -> None:
        legacy_column = "claude_session_id"
        current_column = "worker_session_id"
        legacy_index = f"idx_sessions_{legacy_column}"
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if legacy_column in columns and current_column in columns:
            raise RuntimeError("sessions table has both legacy and worker session columns")
        if legacy_column in columns and current_column not in columns:
            conn.execute(f"ALTER TABLE sessions RENAME COLUMN {legacy_column} TO {current_column}")
        conn.execute(f"DROP INDEX IF EXISTS {legacy_index}")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sessions_worker_session_id
            ON sessions(worker_session_id)
            """
        )

    def _migrate_session_window_names(self, conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "window_name" not in columns:
            return
        for legacy_name, current_name in LEGACY_TO_CURRENT_WINDOW_NAMES.items():
            conn.execute(
                "UPDATE sessions SET window_name = ? WHERE window_name = ?",
                (current_name, legacy_name),
            )

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row is not None else None

    def _update_columns(
        self, conn: sqlite3.Connection, table: str, where_sql: str, where_args: tuple, updates: dict
    ) -> None:
        assignments = ", ".join(f"{column} = ?" for column in updates)
        conn.execute(
            f"UPDATE {table} SET {assignments} WHERE {where_sql}",
            tuple(updates.values()) + where_args,
        )

    def get_event(self, event_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM event_ledger WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def claim_event(
        self,
        event_id: str,
        *,
        envelope_id: str = "",
        source_event_type: str = "",
        conversation_identity: str | None = None,
    ) -> ClaimResult:
        now = utc_now()
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO event_ledger (
                    event_id,
                    envelope_id,
                    source_event_type,
                    conversation_identity,
                    status,
                    first_seen_at,
                    last_updated_at
                ) VALUES (?, ?, ?, ?, 'received', ?, ?)
                """,
                (event_id, envelope_id, source_event_type, conversation_identity, now, now),
            )
            row = conn.execute(
                "SELECT * FROM event_ledger WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return ClaimResult(
            claimed=cursor.rowcount == 1,
            status=row["status"] if row is not None else "missing",
            row=self._row_to_dict(row),
        )

    def update_event(
        self,
        event_id: str,
        *,
        status: str,
        conversation_identity: str | None | object = _UNSET,
        failure_reason: str | None | object = _UNSET,
    ) -> dict | None:
        if status not in EVENT_STATUSES:
            raise ValueError(f"Unsupported event status: {status}")
        updates: dict[str, Any] = {
            "status": status,
            "last_updated_at": utc_now(),
        }
        if conversation_identity is not _UNSET:
            updates["conversation_identity"] = conversation_identity
        if failure_reason is not _UNSET:
            updates["failure_reason"] = failure_reason
        with self._connect(write=True) as conn:
            self._update_columns(conn, "event_ledger", "event_id = ?", (event_id,), updates)
            row = conn.execute(
                "SELECT * FROM event_ledger WHERE event_id = ?",
                (event_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def prune_event_ledger(self, *, older_than_days: int | None = None) -> int:
        retention_days = older_than_days or _default_event_retention_days()
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                "DELETE FROM event_ledger WHERE last_updated_at < ?",
                (cutoff,),
            )
        return cursor.rowcount

    def claim_bootstrap(self, bootstrap_key: str, *, event_id: str, source_event_type: str) -> bool:
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO bootstrap_ledger (
                    bootstrap_key,
                    event_id,
                    source_event_type,
                    created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (bootstrap_key, event_id, source_event_type, utc_now()),
            )
        return cursor.rowcount == 1

    def prune_bootstrap_ledger(self, *, older_than_hours: int | None = None) -> int:
        retention_hours = older_than_hours or _default_bootstrap_retention_hours()
        cutoff = (datetime.now(UTC) - timedelta(hours=retention_hours)).isoformat()
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                "DELETE FROM bootstrap_ledger WHERE created_at < ?",
                (cutoff,),
            )
        return cursor.rowcount

    def get_ack(self, event_id: str, ack_kind: str = "fixed_message") -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ack_ledger WHERE event_id = ? AND ack_kind = ?",
                (event_id, ack_kind),
            ).fetchone()
        return self._row_to_dict(row)

    def claim_ack(self, event_id: str, ack_kind: str = "fixed_message") -> bool:
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO ack_ledger (
                    event_id,
                    ack_kind,
                    status,
                    created_at,
                    updated_at
                ) VALUES (?, ?, 'pending', ?, ?)
                """,
                (event_id, ack_kind, utc_now(), utc_now()),
            )
        return cursor.rowcount == 1

    def update_ack(
        self,
        event_id: str,
        *,
        status: str,
        ack_kind: str = "fixed_message",
        reply_ts: str | None | object = _UNSET,
        closed_by_reply_request_id: str | None | object = _UNSET,
    ) -> dict | None:
        if status not in ACK_STATUSES:
            raise ValueError(f"Unsupported ack status: {status}")
        updates: dict[str, Any] = {
            "status": status,
            "updated_at": utc_now(),
        }
        if reply_ts is not _UNSET:
            updates["reply_ts"] = reply_ts
        if closed_by_reply_request_id is not _UNSET:
            updates["closed_by_reply_request_id"] = closed_by_reply_request_id
        with self._connect(write=True) as conn:
            self._update_columns(
                conn,
                "ack_ledger",
                "event_id = ? AND ack_kind = ?",
                (event_id, ack_kind),
                updates,
            )
            row = conn.execute(
                "SELECT * FROM ack_ledger WHERE event_id = ? AND ack_kind = ?",
                (event_id, ack_kind),
            ).fetchone()
        return self._row_to_dict(row)

    def get_reply(self, reply_request_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM reply_ledger WHERE reply_request_id = ?",
                (reply_request_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def claim_reply(
        self,
        reply_request_id: str,
        *,
        case_id: str,
        payload: Any,
        lease_timeout_seconds: int | None = None,
    ) -> ClaimResult:
        canonical_hash = payload_hash(payload)
        now = utc_now()
        lease_timeout = lease_timeout_seconds or _default_reply_claim_timeout()
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO reply_ledger (
                    reply_request_id,
                    case_id,
                    payload_hash,
                    status,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, 'received', ?, ?)
                """,
                (reply_request_id, case_id, canonical_hash, now, now),
            )
            row = conn.execute(
                "SELECT * FROM reply_ledger WHERE reply_request_id = ?",
                (reply_request_id,),
            ).fetchone()
            if row is None:
                return ClaimResult(claimed=False, status="missing")
            if cursor.rowcount == 1:
                return ClaimResult(claimed=True, status=row["status"], row=self._row_to_dict(row))

            conflict = row["payload_hash"] != canonical_hash or row["case_id"] != case_id
            if (
                not conflict
                and row["status"] == "failed"
                and _reply_failure_is_retryable(row["last_error"])
            ):
                cursor = conn.execute(
                    """
                    UPDATE reply_ledger
                    SET status = 'received',
                        updated_at = ?,
                        last_error = NULL
                    WHERE reply_request_id = ?
                      AND case_id = ?
                      AND payload_hash = ?
                      AND status = 'failed'
                      AND updated_at = ?
                    """,
                    (now, reply_request_id, case_id, canonical_hash, row["updated_at"]),
                )
                if cursor.rowcount == 1:
                    row = conn.execute(
                        "SELECT * FROM reply_ledger WHERE reply_request_id = ?",
                        (reply_request_id,),
                    ).fetchone()
                    if row is not None:
                        return ClaimResult(
                            claimed=True, status=row["status"], row=self._row_to_dict(row)
                        )

            if not conflict and row["status"] == "received":
                updated_at = _parse_utc(row["updated_at"])
                if updated_at is not None and datetime.now(UTC) - updated_at >= timedelta(
                    seconds=lease_timeout
                ):
                    conn.execute(
                        """
                        UPDATE reply_ledger
                        SET updated_at = ?,
                            last_error = NULL
                        WHERE reply_request_id = ?
                          AND case_id = ?
                          AND payload_hash = ?
                          AND status = 'received'
                          AND updated_at = ?
                        """,
                        (now, reply_request_id, case_id, canonical_hash, row["updated_at"]),
                    )
                    row = conn.execute(
                        "SELECT * FROM reply_ledger WHERE reply_request_id = ?",
                        (reply_request_id,),
                    ).fetchone()
                    if row is not None and row["updated_at"] == now:
                        return ClaimResult(
                            claimed=True, status=row["status"], row=self._row_to_dict(row)
                        )
        row_dict = self._row_to_dict(row)
        return ClaimResult(
            claimed=False,
            status=row["status"],
            row=row_dict,
            conflict=conflict,
        )

    def get_turn_attempt(self, turn_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM turn_attempts WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def claim_turn_reply(
        self,
        turn_id: str,
        *,
        reply_request_id: str,
        lease_timeout_seconds: int | None = None,
    ) -> ClaimResult:
        now = utc_now()
        lease_timeout = lease_timeout_seconds or _default_reply_claim_timeout()
        cutoff = (datetime.now(UTC) - timedelta(seconds=lease_timeout)).isoformat()
        with self._connect(write=True) as conn:
            row = conn.execute(
                """
                SELECT
                    turn_attempts.*,
                    sessions.status AS session_status,
                    sessions.active_turn_id AS session_active_turn_id
                FROM turn_attempts
                JOIN sessions
                  ON sessions.channel_id = turn_attempts.channel_id
                 AND sessions.thread_ts = turn_attempts.thread_ts
                WHERE turn_attempts.turn_id = ?
                """,
                (turn_id,),
            ).fetchone()
            if row is None:
                return ClaimResult(claimed=False, status="stale_turn")
            if row["session_status"] not in CASE_REPLY_POSTABLE_SESSION_STATUSES:
                return ClaimResult(claimed=False, status="stale_turn", row=self._row_to_dict(row))
            if row["session_active_turn_id"] != turn_id:
                return ClaimResult(claimed=False, status="stale_turn", row=self._row_to_dict(row))
            if row["completed_at"] or row["completion_action"]:
                return ClaimResult(claimed=False, status="stale_turn", row=self._row_to_dict(row))

            active_reply_request_id = row["active_reply_request_id"]
            active_reply_claimed_at = row["active_reply_claimed_at"]
            claim_is_recoverable = (
                active_reply_request_id == reply_request_id
                or not active_reply_request_id
                or (active_reply_claimed_at is not None and active_reply_claimed_at <= cutoff)
            )
            if not claim_is_recoverable:
                return ClaimResult(claimed=False, status="stale_turn", row=self._row_to_dict(row))

            cursor = conn.execute(
                """
                UPDATE turn_attempts
                SET active_reply_request_id = ?,
                    active_reply_claimed_at = ?
                WHERE turn_id = ?
                  AND completed_at IS NULL
                  AND completion_action IS NULL
                  AND EXISTS (
                    SELECT 1
                    FROM sessions
                    WHERE sessions.channel_id = turn_attempts.channel_id
                      AND sessions.thread_ts = turn_attempts.thread_ts
                      AND sessions.status IN (?, ?)
                      AND sessions.active_turn_id = ?
                  )
                  AND (
                    active_reply_request_id IS NULL
                    OR active_reply_request_id = ?
                    OR active_reply_claimed_at <= ?
                  )
                """,
                (
                    reply_request_id,
                    now,
                    turn_id,
                    *CASE_REPLY_POSTABLE_SESSION_STATUSES,
                    turn_id,
                    reply_request_id,
                    cutoff,
                ),
            )
            row = conn.execute(
                "SELECT * FROM turn_attempts WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        return ClaimResult(
            claimed=cursor.rowcount == 1,
            status="claimed" if cursor.rowcount == 1 else "stale_turn",
            row=self._row_to_dict(row),
        )

    def release_turn_reply_claim(self, turn_id: str, *, reply_request_id: str) -> dict | None:
        with self._connect(write=True) as conn:
            conn.execute(
                """
                UPDATE turn_attempts
                SET active_reply_request_id = NULL,
                    active_reply_claimed_at = NULL
                WHERE turn_id = ?
                  AND active_reply_request_id = ?
                  AND completed_at IS NULL
                """,
                (turn_id, reply_request_id),
            )
            row = conn.execute(
                "SELECT * FROM turn_attempts WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def open_turn_attempt(
        self,
        turn_id: str,
        *,
        channel_id: str,
        thread_ts: str,
        event_id: str,
        owner_user_id: str | None = None,
        started_at: str | None = None,
        payload: Any | None = None,
    ) -> dict | None:
        opened_at = started_at or utc_now()
        payload_json = _json_dumps(payload if payload is not None else {})
        with self._connect(write=True) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO turn_attempts (
                    turn_id,
                    channel_id,
                    thread_ts,
                    event_id,
                    owner_user_id,
                    started_at,
                    completed_at,
                    completion_action,
                    reply_accepted_count,
                    discard_notified_at,
                    payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, 0, NULL, ?)
                """,
                (turn_id, channel_id, thread_ts, event_id, owner_user_id, opened_at, payload_json),
            )
            row = conn.execute(
                "SELECT * FROM turn_attempts WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def get_turn_attempt_payload(self, turn_id: str) -> Any | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM turn_attempts WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        if row is None:
            return None
        raw = row["payload_json"]
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if parsed == {}:
            return None
        return parsed

    def mark_turn_attempt_completed(
        self,
        turn_id: str,
        *,
        completed_at: str | None | object = _UNSET,
        completion_action: str | None | object = _UNSET,
        reply_accepted_count_delta: int = 0,
        discard_notified_at: str | None | object = _UNSET,
    ) -> dict | None:
        updates: dict[str, Any] = {}
        if completed_at is not _UNSET:
            updates["completed_at"] = completed_at
        if completion_action is not _UNSET:
            updates["completion_action"] = completion_action
        if discard_notified_at is not _UNSET:
            updates["discard_notified_at"] = discard_notified_at
        if updates.get("completed_at") is not None:
            updates["active_reply_request_id"] = None
            updates["active_reply_claimed_at"] = None
        with self._connect(write=True) as conn:
            if reply_accepted_count_delta:
                conn.execute(
                    """
                    UPDATE turn_attempts
                    SET reply_accepted_count = reply_accepted_count + ?
                    WHERE turn_id = ?
                    """,
                    (reply_accepted_count_delta, turn_id),
                )
            if updates:
                self._update_columns(conn, "turn_attempts", "turn_id = ?", (turn_id,), updates)
            row = conn.execute(
                "SELECT * FROM turn_attempts WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def mark_turn_attempt_discard_notified(
        self, turn_id: str, *, discard_notified_at: str | None = None
    ) -> dict | None:
        return self.mark_turn_attempt_completed(
            turn_id,
            discard_notified_at=discard_notified_at or utc_now(),
        )

    def prune_old_turn_attempts(
        self,
        *,
        older_than_days: int | None = None,
        now: datetime | None = None,
    ) -> int:
        retention_days = older_than_days or 30
        current = now or datetime.now(UTC)
        cutoff = (current - timedelta(days=retention_days)).isoformat()
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                "DELETE FROM turn_attempts WHERE started_at < ?",
                (cutoff,),
            )
        return cursor.rowcount

    def close_open_turn_attempts_for_session(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        completion_action: str,
    ) -> int:
        now = utc_now()
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                """
                UPDATE turn_attempts
                SET completion_action = ?,
                    completed_at = COALESCE(completed_at, ?)
                WHERE channel_id = ?
                  AND thread_ts = ?
                  AND completed_at IS NULL
                """,
                (completion_action, now, channel_id, thread_ts),
            )
        return cursor.rowcount

    def increment_runtime_counter(self, counter_name: str, amount: int = 1) -> dict | None:
        now = utc_now()
        with self._connect(write=True) as conn:
            conn.execute(
                """
                INSERT INTO runtime_counters (counter_name, counter_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(counter_name) DO UPDATE SET
                    counter_value = runtime_counters.counter_value + excluded.counter_value,
                    updated_at = excluded.updated_at
                """,
                (counter_name, amount, now),
            )
            row = conn.execute(
                "SELECT * FROM runtime_counters WHERE counter_name = ?",
                (counter_name,),
            ).fetchone()
        return self._row_to_dict(row)

    def claim_runtime_counter_slot(self, counter_name: str, limit: int, amount: int = 1) -> dict:
        if limit <= 0:
            return {
                "claimed": False,
                "counter_name": counter_name,
                "counter_value": 0,
                "limit": limit,
            }
        now = utc_now()
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM runtime_counters WHERE counter_name = ?",
                (counter_name,),
            ).fetchone()
            current = int(row["counter_value"]) if row else 0
            if current >= limit:
                return {
                    "claimed": False,
                    "counter_name": counter_name,
                    "counter_value": current,
                    "limit": limit,
                    "updated_at": row["updated_at"] if row else None,
                }
            next_value = current + amount
            conn.execute(
                """
                INSERT INTO runtime_counters (counter_name, counter_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(counter_name) DO UPDATE SET
                    counter_value = excluded.counter_value,
                    updated_at = excluded.updated_at
                """,
                (counter_name, next_value, now),
            )
        return {
            "claimed": True,
            "counter_name": counter_name,
            "counter_value": next_value,
            "limit": limit,
            "updated_at": now,
        }

    def _queue_event_id(self, payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        event_id = payload.get("event_id")
        return event_id if isinstance(event_id, str) and event_id else None

    def _queue_payload_identity(self, payload: Any) -> tuple[str | None, str]:
        return self._queue_event_id(payload), _json_dumps(payload)

    def append_event_queue(
        self,
        payload: Any,
        *,
        conversation_identity: str,
        channel_id: str,
        thread_ts: str,
    ) -> dict | None:
        event_id, payload_json = self._queue_payload_identity(payload)
        now = utc_now()
        with self._connect(write=True) as conn:
            if event_id:
                conn.execute(
                    """
                    INSERT INTO event_queue (
                        event_id,
                        conversation_identity,
                        channel_id,
                        thread_ts,
                        payload_json,
                        status,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        conversation_identity = excluded.conversation_identity,
                        channel_id = excluded.channel_id,
                        thread_ts = excluded.thread_ts,
                        payload_json = excluded.payload_json,
                        status = CASE
                            WHEN event_queue.status = 'done' THEN event_queue.status
                            WHEN event_queue.status = 'failed' THEN event_queue.status
                            ELSE 'queued'
                        END,
                        lease_owner = CASE
                            WHEN event_queue.status IN ('done', 'failed') THEN event_queue.lease_owner
                            ELSE NULL
                        END,
                        lease_expires_at = CASE
                            WHEN event_queue.status IN ('done', 'failed') THEN event_queue.lease_expires_at
                            ELSE NULL
                        END,
                        last_error = CASE
                            WHEN event_queue.status IN ('done', 'failed') THEN event_queue.last_error
                            ELSE NULL
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        event_id,
                        conversation_identity,
                        channel_id,
                        thread_ts,
                        payload_json,
                        now,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM event_queue WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                conn.execute(
                    """
                    UPDATE event_ledger
                    SET status = 'queued',
                        conversation_identity = COALESCE(conversation_identity, ?),
                        last_updated_at = ?,
                        failure_reason = NULL
                    WHERE event_id = ?
                      AND status NOT IN ('done', 'failed')
                    """,
                    (conversation_identity, now, event_id),
                )
                return self._row_to_dict(row)

            conn.execute(
                """
                INSERT INTO event_queue (
                    event_id,
                    conversation_identity,
                    channel_id,
                    thread_ts,
                    payload_json,
                    status,
                    created_at,
                    updated_at
                ) VALUES (NULL, ?, ?, ?, ?, 'queued', ?, ?)
                """,
                (conversation_identity, channel_id, thread_ts, payload_json, now, now),
            )
            row = conn.execute(
                "SELECT * FROM event_queue WHERE queue_id = last_insert_rowid()",
            ).fetchone()
        return self._row_to_dict(row)

    def list_visible_event_queue(self, *, channel_id: str, thread_ts: str) -> list[Any]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM event_queue
                WHERE channel_id = ?
                  AND thread_ts = ?
                  AND status IN ('queued', 'retryable')
                ORDER BY queue_id ASC
                """,
                (channel_id, thread_ts),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def replace_event_queue(
        self,
        items: list[Any],
        *,
        conversation_identity: str,
        channel_id: str,
        thread_ts: str,
    ) -> None:
        desired = [_json_dumps(item) for item in items]
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT queue_id, payload_json FROM event_queue
                WHERE channel_id = ?
                  AND thread_ts = ?
                  AND status IN ('queued', 'retryable')
                ORDER BY queue_id ASC
                """,
                (channel_id, thread_ts),
            ).fetchall()
        current = [row["payload_json"] for row in rows]
        if current == desired:
            return

        now = utc_now()
        retained_queue_ids: list[int] = []
        with self._connect(write=True) as conn:
            for item in items:
                event_id, payload_json = self._queue_payload_identity(item)
                if event_id:
                    existing = conn.execute(
                        "SELECT queue_id FROM event_queue WHERE event_id = ?",
                        (event_id,),
                    ).fetchone()
                    if existing is not None:
                        conn.execute(
                            """
                            UPDATE event_queue
                            SET conversation_identity = ?,
                                channel_id = ?,
                                thread_ts = ?,
                                payload_json = ?,
                                status = CASE
                                    WHEN status = 'done' THEN status
                                    WHEN status = 'failed' THEN status
                                    ELSE 'queued'
                                END,
                                lease_owner = CASE
                                    WHEN status IN ('done', 'failed') THEN lease_owner
                                    ELSE NULL
                                END,
                                lease_expires_at = CASE
                                    WHEN status IN ('done', 'failed') THEN lease_expires_at
                                    ELSE NULL
                                END,
                                last_error = CASE
                                    WHEN status IN ('done', 'failed') THEN last_error
                                    ELSE NULL
                                END,
                                updated_at = ?
                            WHERE event_id = ?
                            """,
                            (
                                conversation_identity,
                                channel_id,
                                thread_ts,
                                payload_json,
                                now,
                                event_id,
                            ),
                        )
                        retained_queue_ids.append(int(existing["queue_id"]))
                    else:
                        cursor = conn.execute(
                            """
                            INSERT INTO event_queue (
                                event_id,
                                conversation_identity,
                                channel_id,
                                thread_ts,
                                payload_json,
                                status,
                                created_at,
                                updated_at
                            ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                            """,
                            (
                                event_id,
                                conversation_identity,
                                channel_id,
                                thread_ts,
                                payload_json,
                                now,
                                now,
                            ),
                        )
                        if cursor.lastrowid is not None:
                            retained_queue_ids.append(int(cursor.lastrowid))
                    conn.execute(
                        """
                        UPDATE event_ledger
                        SET status = 'queued',
                            conversation_identity = COALESCE(conversation_identity, ?),
                            last_updated_at = ?,
                            failure_reason = NULL
                        WHERE event_id = ?
                          AND status NOT IN ('done', 'failed')
                        """,
                        (conversation_identity, now, event_id),
                    )
                    continue
                cursor = conn.execute(
                    """
                    INSERT INTO event_queue (
                        event_id,
                        conversation_identity,
                        channel_id,
                        thread_ts,
                        payload_json,
                        status,
                        created_at,
                        updated_at
                    ) VALUES (NULL, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (conversation_identity, channel_id, thread_ts, payload_json, now, now),
                )
                if cursor.lastrowid is not None:
                    retained_queue_ids.append(int(cursor.lastrowid))

            keep_clause = ""
            args: tuple[Any, ...] = (now, channel_id, thread_ts)
            if retained_queue_ids:
                placeholders = ", ".join("?" for _ in retained_queue_ids)
                keep_clause = f"AND queue_id NOT IN ({placeholders})"
                args = (now, channel_id, thread_ts, *retained_queue_ids)
            conn.execute(
                f"""
                UPDATE event_queue
                SET status = 'done',
                    last_error = 'queue_replaced',
                    updated_at = ?
                WHERE channel_id = ?
                  AND thread_ts = ?
                  AND status IN ('queued', 'retryable')
                  {keep_clause}
                """,
                args,
            )

    def claim_event_queue_head(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        expected_payload: Any,
        lease_owner: str,
        lease_seconds: int = 300,
    ) -> dict | None:
        expected_json = _json_dumps(expected_payload)
        now = utc_now()
        lease_expires_at = (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat()
        with self._connect(write=True) as conn:
            row = conn.execute(
                """
                SELECT * FROM event_queue
                WHERE channel_id = ?
                  AND thread_ts = ?
                  AND status IN ('queued', 'retryable')
                ORDER BY queue_id ASC
                LIMIT 1
                """,
                (channel_id, thread_ts),
            ).fetchone()
            if row is None or row["payload_json"] != expected_json:
                return None
            cursor = conn.execute(
                """
                UPDATE event_queue
                SET status = 'dispatching',
                    lease_owner = ?,
                    lease_expires_at = ?,
                    retry_count = retry_count + 1,
                    last_error = NULL,
                    updated_at = ?
                WHERE queue_id = ?
                  AND status IN ('queued', 'retryable')
                """,
                (lease_owner, lease_expires_at, now, row["queue_id"]),
            )
            if cursor.rowcount != 1:
                return None
            updated = conn.execute(
                "SELECT * FROM event_queue WHERE queue_id = ?",
                (row["queue_id"],),
            ).fetchone()
            if row["event_id"]:
                conn.execute(
                    """
                    UPDATE event_ledger
                    SET status = 'dispatching',
                        last_updated_at = ?,
                        failure_reason = NULL
                    WHERE event_id = ?
                      AND status NOT IN ('done', 'failed')
                    """,
                    (now, row["event_id"]),
                )
        return self._row_to_dict(updated)

    def consume_event_queue_head(
        self, *, channel_id: str, thread_ts: str, expected_payload: Any | None = None
    ) -> bool:
        expected_json = _json_dumps(expected_payload) if expected_payload is not None else None
        now = utc_now()
        with self._connect(write=True) as conn:
            if expected_json is not None:
                row = conn.execute(
                    """
                    SELECT * FROM event_queue
                    WHERE channel_id = ?
                      AND thread_ts = ?
                      AND status = 'dispatching'
                      AND payload_json = ?
                    ORDER BY queue_id ASC
                    LIMIT 1
                    """,
                    (channel_id, thread_ts, expected_json),
                ).fetchone()
                if row is None:
                    row = conn.execute(
                        """
                        SELECT * FROM event_queue
                        WHERE channel_id = ?
                          AND thread_ts = ?
                          AND status IN ('queued', 'retryable')
                        ORDER BY queue_id ASC
                        LIMIT 1
                        """,
                        (channel_id, thread_ts),
                    ).fetchone()
                    if row is None or row["payload_json"] != expected_json:
                        return False
            else:
                row = conn.execute(
                    """
                    SELECT * FROM event_queue
                    WHERE channel_id = ?
                      AND thread_ts = ?
                      AND status IN ('queued', 'retryable')
                    ORDER BY queue_id ASC
                    LIMIT 1
                    """,
                    (channel_id, thread_ts),
                ).fetchone()
                if row is None:
                    return False
            conn.execute(
                """
                UPDATE event_queue
                SET status = 'done',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = NULL,
                    updated_at = ?
                WHERE queue_id = ?
                """,
                (now, row["queue_id"]),
            )
            if row["event_id"]:
                conn.execute(
                    """
                    UPDATE event_ledger
                    SET status = 'done',
                        last_updated_at = ?,
                        failure_reason = NULL
                    WHERE event_id = ?
                      AND status NOT IN ('done', 'failed')
                    """,
                    (now, row["event_id"]),
                )
        return True

    def consume_event_queue_row(self, queue_id: int) -> bool:
        now = utc_now()
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM event_queue WHERE queue_id = ?",
                (queue_id,),
            ).fetchone()
            if row is None:
                return False
            if row["status"] == "done":
                return True
            if row["status"] != "dispatching":
                return False
            conn.execute(
                """
                UPDATE event_queue
                SET status = 'done',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = NULL,
                    updated_at = ?
                WHERE queue_id = ?
                """,
                (now, queue_id),
            )
            if row["event_id"]:
                conn.execute(
                    """
                    UPDATE event_ledger
                    SET status = 'done',
                        last_updated_at = ?,
                        failure_reason = NULL
                    WHERE event_id = ?
                      AND status NOT IN ('done', 'failed')
                    """,
                    (now, row["event_id"]),
                )
        return True

    def mark_event_queue_retryable(self, queue_id: int, *, last_error: str) -> bool:
        now = utc_now()
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM event_queue WHERE queue_id = ?",
                (queue_id,),
            ).fetchone()
            if row is None:
                return False
            cursor = conn.execute(
                """
                UPDATE event_queue
                SET status = 'retryable',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE queue_id = ?
                  AND status IN ('queued', 'retryable', 'dispatching')
                """,
                (last_error, now, queue_id),
            )
            if cursor.rowcount == 1 and row["event_id"]:
                conn.execute(
                    """
                    UPDATE event_ledger
                    SET status = 'retryable',
                        last_updated_at = ?,
                        failure_reason = ?
                    WHERE event_id = ?
                      AND status NOT IN ('done', 'failed')
                    """,
                    (now, last_error, row["event_id"]),
                )
        return cursor.rowcount == 1

    def fail_event_queue_row(self, queue_id: int, *, reason: str) -> bool:
        now = utc_now()
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM event_queue WHERE queue_id = ?",
                (queue_id,),
            ).fetchone()
            if row is None:
                return False
            cursor = conn.execute(
                """
                UPDATE event_queue
                SET status = 'failed',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE queue_id = ?
                  AND status IN ('queued', 'retryable', 'dispatching')
                """,
                (reason, now, queue_id),
            )
            if row["event_id"]:
                conn.execute(
                    """
                    UPDATE event_ledger
                    SET status = 'failed',
                        last_updated_at = ?,
                        failure_reason = ?
                    WHERE event_id = ?
                      AND status != 'done'
                    """,
                    (now, reason, row["event_id"]),
                )
        return cursor.rowcount == 1

    def fail_event_queue_for_session(self, *, channel_id: str, thread_ts: str, reason: str) -> int:
        now = utc_now()
        with self._connect(write=True) as conn:
            rows = conn.execute(
                """
                SELECT * FROM event_queue
                WHERE channel_id = ?
                  AND thread_ts = ?
                  AND status IN ('queued', 'retryable', 'dispatching')
                """,
                (channel_id, thread_ts),
            ).fetchall()
            cursor = conn.execute(
                """
                UPDATE event_queue
                SET status = 'failed',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE channel_id = ?
                  AND thread_ts = ?
                  AND status IN ('queued', 'retryable', 'dispatching')
                """,
                (reason, now, channel_id, thread_ts),
            )
            event_ids = [row["event_id"] for row in rows if row["event_id"]]
            if event_ids:
                placeholders = ", ".join("?" for _ in event_ids)
                conn.execute(
                    f"""
                    UPDATE event_ledger
                    SET status = 'failed',
                        last_updated_at = ?,
                        failure_reason = ?
                    WHERE event_id IN ({placeholders})
                      AND status != 'done'
                    """,
                    (now, reason, *event_ids),
                )
        return cursor.rowcount

    def reset_expired_event_queue_leases(self, *, older_than: str | None = None) -> int:
        cutoff = older_than or utc_now()
        now = utc_now()
        with self._connect(write=True) as conn:
            rows = conn.execute(
                """
                SELECT * FROM event_queue
                WHERE status = 'dispatching'
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (cutoff,),
            ).fetchall()
            cursor = conn.execute(
                """
                UPDATE event_queue
                SET status = 'retryable',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = COALESCE(last_error, 'lease_expired'),
                    updated_at = ?
                WHERE status = 'dispatching'
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (now, cutoff),
            )
            event_ids = [row["event_id"] for row in rows if row["event_id"]]
            if event_ids:
                placeholders = ", ".join("?" for _ in event_ids)
                conn.execute(
                    f"""
                    UPDATE event_ledger
                    SET status = 'retryable',
                        last_updated_at = ?,
                        failure_reason = 'lease_expired'
                    WHERE event_id IN ({placeholders})
                      AND status NOT IN ('done', 'failed')
                    """,
                    (now, *event_ids),
                )
        return cursor.rowcount

    def reset_dispatching_event_queue_leases(
        self, *, reason: str = "startup_reclaim_dispatching"
    ) -> int:
        now = utc_now()
        with self._connect(write=True) as conn:
            rows = conn.execute(
                """
                SELECT * FROM event_queue
                WHERE status = 'dispatching'
                """
            ).fetchall()
            cursor = conn.execute(
                """
                UPDATE event_queue
                SET status = 'retryable',
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE status = 'dispatching'
                """,
                (reason, now),
            )
            event_ids = [row["event_id"] for row in rows if row["event_id"]]
            if event_ids:
                placeholders = ", ".join("?" for _ in event_ids)
                conn.execute(
                    f"""
                    UPDATE event_ledger
                    SET status = 'retryable',
                        last_updated_at = ?,
                        failure_reason = ?
                    WHERE event_id IN ({placeholders})
                      AND status NOT IN ('done', 'failed')
                    """,
                    (now, reason, *event_ids),
                )
        return cursor.rowcount

    def list_resumable_event_queue(self, *, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM event_queue
                WHERE status IN ('queued', 'retryable')
                ORDER BY queue_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_runtime_counter(self, counter_name: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runtime_counters WHERE counter_name = ?",
                (counter_name,),
            ).fetchone()
        return self._row_to_dict(row)

    def mark_reply_posted(
        self,
        reply_request_id: str,
        *,
        channel_id: str,
        thread_ts: str,
        reply_ts: str,
    ) -> dict | None:
        with self._connect(write=True) as conn:
            self._update_columns(
                conn,
                "reply_ledger",
                "reply_request_id = ?",
                (reply_request_id,),
                {
                    "status": "posted",
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "reply_ts": reply_ts,
                    "last_error": None,
                    "updated_at": utc_now(),
                },
            )
            row = conn.execute(
                "SELECT * FROM reply_ledger WHERE reply_request_id = ?",
                (reply_request_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def mark_reply_posted_unknown(
        self,
        reply_request_id: str,
        *,
        channel_id: str,
        thread_ts: str,
        reply_ts: str = "",
        last_error: str,
    ) -> dict | None:
        with self._connect(write=True) as conn:
            self._update_columns(
                conn,
                "reply_ledger",
                "reply_request_id = ?",
                (reply_request_id,),
                {
                    "status": "posted_unknown",
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "reply_ts": reply_ts,
                    "last_error": last_error,
                    "updated_at": utc_now(),
                },
            )
            row = conn.execute(
                "SELECT * FROM reply_ledger WHERE reply_request_id = ?",
                (reply_request_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def mark_reply_failed(self, reply_request_id: str, *, last_error: str) -> dict | None:
        with self._connect(write=True) as conn:
            conn.execute(
                """
                UPDATE reply_ledger
                SET status = CASE WHEN status = 'posted' THEN status ELSE 'failed' END,
                    last_error = ?,
                    updated_at = ?
                WHERE reply_request_id = ?
                """,
                (last_error, utc_now(), reply_request_id),
            )
            row = conn.execute(
                "SELECT * FROM reply_ledger WHERE reply_request_id = ?",
                (reply_request_id,),
            ).fetchone()
        return self._row_to_dict(row)
