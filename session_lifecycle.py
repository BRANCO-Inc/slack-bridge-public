import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import cast

from bridge_logging import get_logger
from config import (
    IDLE_TIMEOUT_HOURS,
    INPUT_WAIT_TIMEOUT,
    PROCESS_EXITED_TEXT,
    SESSION_ABNORMAL_EXIT_TEXT,
    STALE_TIMEOUT,
    STATE_DB_PATH,
    TERMINAL_SESSION_RETENTION,
    TEST_MODE,
    TURN_COMPLETION_TIMEOUT,
    TURN_TIMEOUT_TEXT,
    WAIT_TIMEOUT_TEXT,
)
from input_detector import InputDetector
from message_dispatch import MessageDispatch
from session import (
    ACTIVE,
    ALLOWED_TRANSITIONS,
    Session,
    Status,
    clear_turn_fields,
    coerce_status,
    now_utc,
)
from session_store import SessionStore
from tmux_gateway import TmuxGateway
from turn_context import cleanup_turn_context
from worker_process import WorkerProcess

logger = get_logger(__name__)
IDLE_TIMEOUT_WINDOW = timedelta(hours=IDLE_TIMEOUT_HOURS)


def _coerce_now(now: str | datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if isinstance(now, datetime):
        return now.astimezone(UTC)
    return datetime.fromisoformat(now).astimezone(UTC)


def reap_idle_sessions(
    *,
    store: SessionStore | None = None,
    tmux: TmuxGateway | None = None,
    now: str | datetime | None = None,
) -> int:
    store = store or SessionStore(STATE_DB_PATH)
    tmux = tmux or TmuxGateway()
    if not hasattr(store, "list_all") or not hasattr(store, "transition"):
        return 0
    current = _coerce_now(now)
    reaped = 0
    for session in store.list_all():
        if getattr(session.status, "value", session.status) != Status.IDLE.value:
            continue
        if not session.last_activity_at:
            continue
        last_activity = _coerce_now(session.last_activity_at)
        if current - last_activity < IDLE_TIMEOUT_WINDOW:
            continue
        updated = store.transition(
            session.conversation_identity,
            Status.IDLE,
            Status.KILLED,
            updates={
                "last_activity_at": current.isoformat(),
                "pane_id": None,
                **clear_turn_fields(),
            },
        )
        if updated is None:
            continue
        if session.pane_id:
            with suppress(Exception):
                tmux.kill_pane(session.pane_id)
        reaped += 1
    return reaped


class SessionLifecycle:
    def __init__(
        self,
        tmux: TmuxGateway,
        store: SessionStore,
        worker_process: WorkerProcess,
        dispatch: MessageDispatch,
        input_detector: InputDetector,
        post_to_slack_fn,
        state=None,
    ):
        self.tmux = tmux
        self.store = store
        self.worker_process = worker_process
        self.dispatch = dispatch
        self.input_detector = input_detector
        self.post_to_slack = post_to_slack_fn
        self.state = state

    def _post_to_slack_with_retry(self, channel_id: str, thread_ts: str, text: str):
        delay = 1
        last_result = {"ok": False, "error": "slack_post_failed"}
        for attempt in range(3):
            result = self.post_to_slack(channel_id, thread_ts, text)
            if not isinstance(result, dict) or result.get("ok", True):
                return result
            last_result = result
            if attempt < 2:
                if not TEST_MODE:
                    time.sleep(delay)
                delay *= 2
        logger.error(
            "Slack post failed after retries channel=%s thread=%s error=%s",
            channel_id,
            thread_ts,
            last_result.get("error", "slack_post_failed"),
        )
        return last_result

    def _manual_transition(self, session: Session, new_status: Status, updates: dict) -> Session:
        loaded = None
        try:
            loaded = self.store.load(session.conversation_identity)
        except Exception:
            loaded = None
        latest = loaded if isinstance(loaded, Session) else session
        latest.status = new_status
        for update_key, value in updates.items():
            setattr(latest, update_key, value)
        self.store.save(latest)
        return latest

    def _finalize_turn_attempt(
        self,
        session: Session,
        completion_action: str,
        *,
        turn_id: str | None = None,
    ) -> None:
        effective_turn_id = turn_id or session.active_turn_id
        if self.state is None or not effective_turn_id:
            return
        try:
            self.state.mark_turn_attempt_completed(
                effective_turn_id,
                completed_at=now_utc(),
                completion_action=completion_action,
            )
        except Exception as exc:
            logger.warning(
                "Failed to finalize turn attempt turn_id=%s action=%s: %s",
                effective_turn_id,
                completion_action,
                exc,
            )

    def _close_open_turn_attempts_for_session(
        self,
        session: Session,
        completion_action: str,
    ) -> None:
        if self.state is None or not session.channel_id or not session.thread_ts:
            return
        try:
            self.state.close_open_turn_attempts_for_session(
                channel_id=session.channel_id,
                thread_ts=session.thread_ts,
                completion_action=completion_action,
            )
        except Exception as exc:
            logger.warning(
                "Failed to close open turn attempts for identity=%s action=%s: %s",
                session.conversation_identity,
                completion_action,
                exc,
            )

    def finalize_session_turns(
        self,
        session: Session,
        completion_action: str,
        *,
        turn_id: str | None = None,
        cleanup_artifact: bool = True,
    ) -> None:
        effective_turn_id = turn_id or session.active_turn_id
        if effective_turn_id:
            self._finalize_turn_attempt(session, completion_action, turn_id=effective_turn_id)
            if cleanup_artifact:
                cleanup_turn_context(effective_turn_id)
        self._close_open_turn_attempts_for_session(session, completion_action)

    def _fail_active_queue_item(
        self,
        queue_id: int | None,
        *,
        reason: str,
        identity: str,
    ) -> None:
        if not queue_id:
            return
        try:
            self.store.fail_queue_item(queue_id, reason=reason)
        except Exception as exc:
            logger.warning(
                "Failed to mark active queue failed identity=%s queue_id=%s reason=%s: %s",
                identity,
                queue_id,
                reason,
                exc,
            )

    def clear_pane_after_teardown(self, session: Session) -> Session:
        if not session.pane_id:
            return session
        persisted = self.store.update_fields(
            session.conversation_identity,
            {
                "pane_id": None,
                "last_activity_at": now_utc(),
            },
        )
        updated = persisted if isinstance(persisted, Session) else session
        updated.pane_id = None
        return updated

    def _soft_fail_transition(self, session: Session, reason: str) -> Session:
        logger.error(
            "Invalid session transition: identity=%s status=%s reason=%s pane_id=%s",
            session.conversation_identity,
            session.status.value,
            reason,
            session.pane_id or "none",
        )
        if session.pane_id:
            try:
                self.tmux.kill_pane(session.pane_id)
            except Exception as exc:
                logger.warning("kill_pane failed during soft fail for %s: %s", session.pane_id, exc)
        updated = self.store.update_fields(
            session.conversation_identity,
            {
                "status": Status.ERROR,
                "failure_reason": reason,
                "input_wait": None,
                "last_activity_at": now_utc(),
                **clear_turn_fields(),
            },
        )
        if not isinstance(updated, Session):
            updated = self._manual_transition(
                session,
                Status.ERROR,
                {
                    "failure_reason": reason,
                    "input_wait": None,
                    "last_activity_at": now_utc(),
                    **clear_turn_fields(),
                },
            )
        self.input_detector.clear_session(updated.thread_ts)
        self.finalize_session_turns(session, "error")
        self._post_to_slack_with_retry(
            updated.channel_id,
            updated.thread_ts,
            SESSION_ABNORMAL_EXIT_TEXT,
        )
        return updated

    def transition(
        self,
        session: Session,
        new_status: Status | str,
        *,
        reason: str | None = None,
        expected_status: Status | str | set[Status] | None = None,
        updates: dict | None = None,
        teardown: bool = False,
    ) -> Session | None:
        target = coerce_status(new_status)
        try:
            loaded = self.store.load(session.conversation_identity)
        except Exception:
            loaded = None
        current = loaded if isinstance(loaded, Session) else session
        expected = expected_status
        if expected is None:
            expected_statuses = {current.status}
        elif isinstance(expected, str) or hasattr(expected, "value"):
            expected_statuses = {coerce_status(cast("Status | str", expected))}
        else:
            expected_statuses = {coerce_status(status) for status in expected}
        if current.status not in expected_statuses:
            return None
        allowed_targets = ALLOWED_TRANSITIONS.get(current.status, set())
        if target not in allowed_targets:
            return self._soft_fail_transition(
                current, reason or f"invalid_transition:{current.status.value}->{target.value}"
            )
        merged_updates = {"last_activity_at": now_utc()}
        if updates:
            merged_updates.update(updates)
        updated = self.store.transition(
            current.conversation_identity,
            current.status,
            target,
            updates=merged_updates,
        )
        if updated is None:
            return None
        if teardown:
            self._teardown_runtime(updated)
        return updated

    def _teardown_runtime(self, session: Session) -> None:
        if session.pane_id:
            try:
                self.tmux.kill_pane(session.pane_id)
            except Exception as exc:
                logger.warning("kill_pane failed for %s: %s", session.pane_id, exc)
        self.input_detector.clear_session(session.thread_ts)

    def _suspend_with_reason(
        self,
        session: Session,
        expected_status: Status | set[Status],
        reason: str,
    ) -> Session | None:
        active_turn_id = session.active_turn_id
        updated = self.transition(
            session,
            Status.SUSPENDED,
            reason=reason,
            expected_status=expected_status,
            updates={
                "failure_reason": reason,
                "input_wait": None,
                **clear_turn_fields(),
            },
            teardown=True,
        )
        if updated is not None:
            self.finalize_session_turns(session, reason or "suspended", turn_id=active_turn_id)
            updated = self.clear_pane_after_teardown(updated)
            logger.info(
                "Session suspended: identity=%s status=%s reason=%s",
                updated.conversation_identity,
                updated.status.value,
                reason,
            )
        return updated

    def suspend(self, session: Session, *, reason: str | None = None):
        active_turn_id = session.active_turn_id
        completion_action = reason or "suspended"
        updated = self.transition(
            session,
            Status.SUSPENDED,
            reason=reason,
            expected_status={Status.READY, Status.IDLE, Status.WAITING, Status.BUSY},
            updates={
                "failure_reason": reason,
                "input_wait": None,
                **clear_turn_fields(),
            },
            teardown=True,
        )
        if updated is not None:
            self.finalize_session_turns(session, completion_action, turn_id=active_turn_id)
            updated = self.clear_pane_after_teardown(updated)
        return updated

    def cancel(self, session: Session, *, reason: str = "user_cancel"):
        current = self.store.load(session.conversation_identity) or session
        if current.status not in {Status.BUSY, Status.WAITING}:
            return None
        active_turn_id = current.active_turn_id
        if current.pane_id:
            try:
                self.tmux.interrupt(current.pane_id)
            except Exception as exc:
                logger.warning("interrupt failed for %s: %s", current.pane_id, exc)
        updated = self.transition(
            current,
            Status.IDLE,
            reason=reason,
            expected_status={Status.BUSY, Status.WAITING},
            updates={
                "failure_reason": reason,
                "input_wait": None,
                **clear_turn_fields(),
            },
            teardown=False,
        )
        if updated is None:
            latest = self.store.load(current.conversation_identity) or current
            if latest.status in {Status.BUSY, Status.WAITING}:
                updated = self._manual_transition(
                    latest,
                    Status.IDLE,
                    {
                        "failure_reason": reason,
                        "input_wait": None,
                        "last_activity_at": now_utc(),
                        **clear_turn_fields(),
                    },
                )
            elif latest.status is Status.IDLE and latest.active_turn_id is None:
                return latest
            else:
                return None
        self.finalize_session_turns(current, "cancelled", turn_id=active_turn_id)
        self.input_detector.clear_session(updated.thread_ts)
        return updated

    def kill(self, session: Session):
        current = self.store.load(session.conversation_identity) or session
        active_turn_id = current.active_turn_id
        updated = self.transition(
            current,
            Status.KILLED,
            expected_status=ACTIVE | {Status.SUSPENDED},
            updates=clear_turn_fields(),
            teardown=False,
        )
        if updated is None:
            return None
        if current.pane_id:
            try:
                self.tmux.kill_pane(current.pane_id)
            except Exception as exc:
                logger.warning("kill_pane failed for %s: %s", current.pane_id, exc)
        self.input_detector.clear_session(updated.thread_ts)
        persisted = self.store.update_fields(
            updated.conversation_identity,
            {
                "pane_id": None,
                "last_activity_at": now_utc(),
            },
        )
        final_session = persisted if isinstance(persisted, Session) else updated
        final_session.pane_id = None
        self.finalize_session_turns(current, "killed", turn_id=active_turn_id)
        return final_session

    def terminate_for_restart(self, session: Session, *, reason: str = "graceful_shutdown_restart"):
        current = self.store.load(session.conversation_identity) or session
        active_turn_id = current.active_turn_id
        active_turn_queue_id = current.active_turn_queue_id
        updated = self.transition(
            current,
            Status.SUSPENDED,
            reason=reason,
            expected_status=ACTIVE,
            updates={
                "failure_reason": reason,
                "input_wait": None,
                **clear_turn_fields(),
            },
            teardown=True,
        )
        if updated is None:
            return None
        if active_turn_queue_id:
            try:
                self.store.mark_queue_retryable(active_turn_queue_id, last_error=reason)
            except Exception as exc:
                logger.warning(
                    "Failed to mark active queue retryable during restart identity=%s queue_id=%s: %s",
                    current.conversation_identity,
                    active_turn_queue_id,
                    exc,
                )
        self.finalize_session_turns(current, reason, turn_id=active_turn_id)
        return self.clear_pane_after_teardown(updated)

    def cleanup_stale(self):
        now = datetime.now(UTC)
        for session in self.store.list_all():
            try:
                if self._handle_orphaned(session):
                    continue
                if self._handle_busy_timeout(session, now):
                    continue
                if self._handle_wait_timeout(session, now):
                    continue
                if self._handle_finished_timeout(session, now):
                    continue
                self._handle_suspend_timeout(session, now)
                self._handle_terminal_cleanup(session, now)
            except Exception as exc:
                logger.exception(
                    "cleanup_stale failed for identity=%s status=%s: %s",
                    session.conversation_identity or session.thread_ts,
                    session.status.value,
                    exc,
                )

    def _handle_orphaned(self, session: Session) -> bool:
        if not session.is_active() or not session.pane_id:
            return False
        if self.tmux.pane_exists(session.pane_id):
            return False
        active_turn_id = session.active_turn_id
        updated = self.transition(
            session,
            Status.SUSPENDED,
            reason="orphaned_pane_missing",
            expected_status=session.status,
            updates={
                "failure_reason": "orphaned_pane_missing",
                "input_wait": None,
                **clear_turn_fields(),
            },
            teardown=True,
        )
        if updated is None:
            return True
        self.finalize_session_turns(session, "orphaned_pane_missing", turn_id=active_turn_id)
        updated = self.clear_pane_after_teardown(updated)
        self.post_to_slack(
            updated.channel_id,
            updated.thread_ts,
            PROCESS_EXITED_TEXT,
        )
        return True

    def _handle_busy_timeout(self, session: Session, now: datetime) -> bool:
        if session.status is not Status.BUSY:
            return False
        elapsed = self._elapsed_seconds(session, now)
        if not elapsed or elapsed <= TURN_COMPLETION_TIMEOUT:
            return False
        active_turn_id = session.active_turn_id
        active_turn_queue_id = session.active_turn_queue_id
        updated = self.transition(
            session,
            Status.SUSPENDED,
            reason="busy_timeout",
            expected_status=Status.BUSY,
            updates={
                "failure_reason": "busy_timeout",
                "input_wait": None,
                **clear_turn_fields(),
            },
            teardown=True,
        )
        if updated is None:
            return False
        self._fail_active_queue_item(
            active_turn_queue_id,
            reason="busy_timeout",
            identity=session.conversation_identity,
        )
        self.finalize_session_turns(session, "busy_timeout", turn_id=active_turn_id)
        updated = self.clear_pane_after_teardown(updated)
        logger.info(
            "Busy timeout: identity=%s elapsed=%.0fs timeout=%ss",
            updated.conversation_identity,
            elapsed,
            TURN_COMPLETION_TIMEOUT,
        )
        self.post_to_slack(
            updated.channel_id,
            updated.thread_ts,
            TURN_TIMEOUT_TEXT,
        )
        return True

    def _handle_wait_timeout(self, session: Session, now: datetime) -> bool:
        if session.status is not Status.WAITING:
            return False
        elapsed = self._elapsed_seconds(session, now)
        if not elapsed or elapsed <= INPUT_WAIT_TIMEOUT:
            return False
        active_turn_id = session.active_turn_id
        active_turn_queue_id = session.active_turn_queue_id
        suspended = self.transition(
            session,
            Status.SUSPENDED,
            reason="wait_timeout",
            expected_status=Status.WAITING,
            updates={
                "failure_reason": "wait_timeout",
                "input_wait": None,
                **clear_turn_fields(),
            },
            teardown=True,
        )
        if suspended is None:
            return False
        self._fail_active_queue_item(
            active_turn_queue_id,
            reason="wait_timeout",
            identity=session.conversation_identity,
        )
        self.finalize_session_turns(session, "wait_timeout", turn_id=active_turn_id)
        suspended = self.clear_pane_after_teardown(suspended)
        self.post_to_slack(
            suspended.channel_id,
            suspended.thread_ts,
            WAIT_TIMEOUT_TEXT,
        )
        return True

    def _handle_suspend_timeout(self, session: Session, now: datetime):
        if session.status is not Status.WAITING:
            return
        elapsed = self._elapsed_seconds(session, now)
        if elapsed and elapsed > STALE_TIMEOUT:
            self.suspend(session)

    def _handle_finished_timeout(self, session: Session, now: datetime) -> bool:
        if session.status not in {Status.IDLE, Status.READY}:
            return False
        elapsed = self._elapsed_seconds(session, now)
        if not elapsed or elapsed <= STALE_TIMEOUT:
            return False
        killed = self.kill(session)
        if killed is None:
            logger.warning(
                "finished session timeout kill skipped: identity=%s status=%s",
                session.conversation_identity,
                session.status.value,
            )
            return False
        logger.info(
            "Finished session killed after timeout: identity=%s elapsed=%.0fs timeout=%ss",
            killed.conversation_identity,
            elapsed,
            STALE_TIMEOUT,
        )
        return True

    def _handle_terminal_cleanup(self, session: Session, now: datetime):
        if session.status not in {Status.ERROR, Status.KILLED}:
            return
        elapsed = self._elapsed_seconds(session, now, "last_activity_at", "created_at")
        if not elapsed or elapsed <= TERMINAL_SESSION_RETENTION:
            return
        self.store.delete(session.conversation_identity)
        self.input_detector.clear_session(session.thread_ts)

    def _elapsed_seconds(self, session: Session, now: datetime, *fields: str) -> float | None:
        for field in fields or ("last_activity_at",):
            value = getattr(session, field, "")
            if not value:
                continue
            try:
                last_dt = datetime.fromisoformat(value)
            except ValueError, TypeError:
                continue
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=UTC)
            return (now - last_dt).total_seconds()
        return None
