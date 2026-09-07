import time
import uuid

from bridge_logging import get_logger
from config import (
    ANSWER_SEND_FAILED_TEXT,
    EMPTY_ANSWER_TEXT,
    QUEUE_APPEND_FAILED_TEXT,
    QUEUE_RETRY_TEXT,
    QUEUE_SEND_FAILED_TEXT,
    SESSION_ERROR_TEXT,
    SESSION_NOT_FOUND_TEXT,
    STATE_UPDATE_FAILED_TEXT,
    UNKNOWN_STATUS_TEXT,
)
from input_detector import InputDetector
from interactive_handler import InteractiveHandler
from session import Session, Status, activate_turn_fields, clear_turn_fields, now_utc
from session_store import SessionStore
from tmux_gateway import TmuxGateway
from turn_context import TurnArtifacts, write_turn_context
from worker_payload import build_worker_payload

logger = get_logger(__name__)


class MessageDispatch:
    def __init__(
        self,
        tmux: TmuxGateway,
        store: SessionStore,
        interactive: InteractiveHandler,
        input_detector: InputDetector,
        post_to_slack_fn,
        recover_fn,
        state=None,
    ):
        self.tmux = tmux
        self.store = store
        self.interactive = interactive
        self.input_detector = input_detector
        self.post_to_slack = post_to_slack_fn
        self.recover_fn = recover_fn
        self.state = state
        self._active_turn_payloads: dict[str, dict | str] = {}

    @staticmethod
    def _valid_turn_payload(payload: dict | str | None) -> bool:
        return payload is not None and payload != {}

    def get_active_turn_payload(self, turn_id: str | None) -> dict | str | None:
        if not turn_id:
            return None
        stored = self._active_turn_payloads.get(turn_id)
        if stored is not None:
            return stored
        if self.state is None:
            return None
        return self.state.get_turn_attempt_payload(turn_id)

    def _remember_turn_payload(self, session: Session, payload: dict | str) -> None:
        if session.active_turn_id and self._valid_turn_payload(payload):
            self._active_turn_payloads[session.active_turn_id] = payload

    def _forget_turn_payload(self, session: Session) -> None:
        if session.active_turn_id:
            self._active_turn_payloads.pop(session.active_turn_id, None)

    def _open_turn_attempt(self, session: Session, payload: dict | str | None = None) -> None:
        if self.state is None or not session.active_turn_id:
            return
        self.state.open_turn_attempt(
            session.active_turn_id,
            channel_id=session.channel_id,
            thread_ts=session.thread_ts,
            event_id=session.active_turn_event_id or "",
            owner_user_id=session.active_turn_owner_user_id,
            started_at=session.active_turn_started_at,
            payload=payload if self._valid_turn_payload(payload) else None,
        )

    def _close_turn_attempt_after_send_failure(self, session: Session) -> None:
        if self.state is None or not session.active_turn_id:
            return
        self.state.mark_turn_attempt_completed(
            session.active_turn_id,
            completed_at=now_utc(),
            completion_action="tmux_send_failed",
        )

    def route(self, conversation_identity: str, payload: dict | str, say):
        session = self.store.load(conversation_identity)
        if not session:
            say(SESSION_NOT_FOUND_TEXT)
            return
        if session.status is Status.WAITING:
            self._route_waiting(session, payload, say)
            return
        if session.status is Status.SUSPENDED:
            self.recover_fn(session, payload, say)
            return
        if session.status in {Status.READY, Status.IDLE}:
            self._route_active(session, payload, say)
            return
        if session.status in {Status.BUSY, Status.CREATING, Status.STARTING}:
            self._route_queue(session, payload, say)
            return
        if session.status is Status.ERROR:
            say(SESSION_ERROR_TEXT)
            return
        if session.status is not Status.KILLED:
            say(UNKNOWN_STATUS_TEXT.format(status=session.status.value))

    def _route_waiting(self, session: Session, payload: dict | str, say):
        if isinstance(payload, str):
            answer_text = payload
            event_id = None
            owner_user_id = session.user_id
        elif payload.get("normalized_event_type") in {
            "new_message",
            "thread_message",
            "direct_message",
        }:
            answer_text = (payload.get("source_message") or {}).get("text", "").strip()
            event_id = payload.get("event_id")
            owner_user_id = (payload.get("actor") or {}).get("user_id") or session.user_id
            if not answer_text:
                say(EMPTY_ANSWER_TEXT)
                return
        else:
            self._route_queue(session, payload, say)
            return
        ok = self.interactive.handle_user_reply(
            session,
            answer_text,
            event_id=event_id,
            owner_user_id=owner_user_id,
            payload=payload,
        )
        if ok:
            self.input_detector.clear_session(session.thread_ts)
            return
        say(ANSWER_SEND_FAILED_TEXT)

    def _route_active(self, session: Session, payload: dict | str, say):
        busy_session = self.store.transition(
            session.conversation_identity,
            {Status.READY, Status.IDLE},
            Status.BUSY,
            updates=self._turn_updates(session, payload),
        )
        if busy_session is None:
            self._handle_route_race(session, payload, say)
            return
        self._open_turn_attempt(busy_session, payload)
        self._remember_turn_payload(busy_session, payload)
        turn_artifacts = None
        if busy_session.active_turn_id and busy_session.window_name:
            turn_artifacts = write_turn_context(busy_session, turn_id=busy_session.active_turn_id)
        if self._send_payload(busy_session, payload, turn_artifacts=turn_artifacts):
            return
        self._rollback_active_message(busy_session, payload, say)

    def _handle_route_race(self, session: Session, payload: dict | str, say):
        latest = self.store.load(session.conversation_identity)
        if latest and latest.status is not session.status:
            self.route(session.conversation_identity, payload, say)
            return
        say(STATE_UPDATE_FAILED_TEXT)

    def _route_queue(self, session: Session, payload: dict | str, say):
        updated = self.store.append_to_queue(session.conversation_identity, payload)
        if updated is None:
            say(QUEUE_APPEND_FAILED_TEXT)

    def complete_turn(self, session: Session, *, action: str = "done") -> Session | None:
        current = self.store.load(session.conversation_identity) or session
        if action == "wait":
            if current.status is Status.WAITING:
                return current
            if not self._complete_claimed_queue(current):
                return None
            self._forget_turn_payload(current)
            waiting = self.store.transition(
                current.conversation_identity,
                Status.BUSY,
                Status.WAITING,
                updates={
                    **clear_turn_fields(),
                    "last_activity_at": now_utc(),
                    "input_wait": {
                        "type": "free_input",
                        "response_format": "text",
                        "options": [],
                    },
                },
            )
            if waiting is not None:
                self.input_detector.clear_session(waiting.thread_ts)
            return waiting

        if current.status is Status.WAITING:
            self._forget_turn_payload(current)
            cleared = self.store.transition(
                current.conversation_identity,
                Status.WAITING,
                Status.IDLE,
                updates={
                    **clear_turn_fields(),
                    "last_activity_at": now_utc(),
                    "input_wait": None,
                },
            )
            if cleared is None:
                latest = self.store.load(current.conversation_identity)
                if latest and latest.status is Status.IDLE and latest.active_turn_id is None:
                    return latest
                return None
            self.input_detector.clear_session(cleared.thread_ts)
            if cleared.event_queue:
                self._drain_queue(cleared)
            return cleared

        if not self._complete_claimed_queue(current):
            return None
        self._forget_turn_payload(current)
        cleared = self.store.transition(
            current.conversation_identity,
            Status.BUSY,
            Status.IDLE,
            updates={
                **clear_turn_fields(),
                "last_activity_at": now_utc(),
                "input_wait": None,
            },
        )
        if cleared is None:
            latest = self.store.load(current.conversation_identity)
            if latest and latest.status is Status.IDLE and latest.active_turn_id is None:
                return latest
            return None
        self.input_detector.clear_session(cleared.thread_ts)
        if cleared.event_queue:
            self._drain_queue(cleared)
        return cleared

    def _complete_claimed_queue(self, session: Session) -> bool:
        if not session.active_turn_queue_id:
            return True
        if not self.store.consume_queue_item(session.active_turn_queue_id):
            logger.warning(
                "queue item completion failed for %s queue_id=%s",
                session.thread_ts,
                session.active_turn_queue_id,
            )
            return False
        return True

    def _drain_queue(self, session: Session):
        if not session.event_queue:
            logger.warning("queue drain requested with empty queue for %s", session.thread_ts)
            return
        next_msg = session.event_queue[0]
        lease_owner = f"turn-drain:{session.conversation_identity}:{uuid.uuid4().hex}"
        claimed = self.store.claim_queue_head(
            session.conversation_identity,
            next_msg,
            lease_owner=lease_owner,
        )
        if claimed is None:
            logger.warning("queue drain claim failed for %s", session.thread_ts)
            return
        busy_session = self.store.transition(
            session.conversation_identity,
            Status.IDLE,
            Status.BUSY,
            updates=self._turn_updates(session, next_msg, queue_id=claimed["queue_id"]),
        )
        if busy_session is None:
            logger.warning("queue drain BUSY transition failed for %s", session.thread_ts)
            self.store.mark_queue_retryable(
                claimed["queue_id"], last_error="busy_transition_failed"
            )
            return
        self._open_turn_attempt(busy_session, next_msg)
        self._remember_turn_payload(busy_session, next_msg)
        turn_artifacts = None
        if busy_session.active_turn_id and busy_session.window_name:
            turn_artifacts = write_turn_context(busy_session, turn_id=busy_session.active_turn_id)
        if not self._send_payload(busy_session, next_msg, turn_artifacts=turn_artifacts):
            self._close_turn_attempt_after_send_failure(busy_session)
            self.store.transition(
                busy_session.conversation_identity,
                Status.BUSY,
                Status.IDLE,
                updates={
                    **clear_turn_fields(),
                    "last_activity_at": now_utc(),
                },
            )
            self.post_to_slack(
                busy_session.channel_id,
                busy_session.thread_ts,
                QUEUE_SEND_FAILED_TEXT,
            )
            self.store.mark_queue_retryable(claimed["queue_id"], last_error="tmux_send_failed")
            return
        logger.info(
            "Queue dispatched for %s queue_id=%s", busy_session.window_name, claimed["queue_id"]
        )

    def _send_payload(
        self,
        session: Session,
        text: dict | str,
        *,
        turn_artifacts: TurnArtifacts | None = None,
    ) -> bool:
        if not session.pane_id:
            logger.error("tmux send rejected without pane_id for %s", session.conversation_identity)
            return False
        if not session.window_name:
            logger.error(
                "tmux send rejected without window_name for %s", session.conversation_identity
            )
            return False
        target = self.tmux.resolve_target(session.window_name, session.pane_id)
        for attempt in range(3):
            try:
                self.tmux.send_text_and_enter(
                    target,
                    self._payload_to_text(session, text, turn_artifacts=turn_artifacts),
                )
                return True
            except Exception as e:
                logger.error(
                    "tmux send failed for %s attempt=%s: %s", session.thread_ts, attempt + 1, e
                )
                if attempt < 2:
                    time.sleep(1)
        return False

    def _rollback_active_message(self, session: Session, text: dict | str, say):
        self._close_turn_attempt_after_send_failure(session)
        self._forget_turn_payload(session)
        self.store.append_to_queue(session.conversation_identity, text)
        self.store.transition(
            session.conversation_identity,
            Status.BUSY,
            Status.IDLE,
            updates={
                **clear_turn_fields(),
                "last_activity_at": now_utc(),
            },
        )
        say(QUEUE_RETRY_TEXT, thread_ts=session.thread_ts)

    def _turn_updates(
        self, session: Session, payload: dict | str, *, queue_id: int | None = None
    ) -> dict:
        owner_user_id = session.user_id
        event_id = None
        if isinstance(payload, dict):
            owner_user_id = (payload.get("actor") or {}).get("user_id") or session.user_id
            event_id = payload.get("event_id")
        return activate_turn_fields(
            session,
            turn_id=uuid.uuid4().hex,
            event_id=event_id,
            owner_user_id=owner_user_id,
            queue_id=queue_id,
        )

    def _payload_to_text(
        self,
        session: Session,
        payload: dict | str,
        *,
        turn_artifacts: TurnArtifacts | None = None,
    ) -> str:
        return build_worker_payload(
            session,
            payload,
            turn_id=session.active_turn_id or "",
            reply_command_path=(
                turn_artifacts.reply_command_path if turn_artifacts is not None else ""
            ),
            conversation_session=session,
        )
