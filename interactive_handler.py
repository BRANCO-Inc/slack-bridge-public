"""入力待ち検出時のSlack通知と回答転送"""

import re
import uuid

from bridge_logging import get_logger
from input_detector import InputWaitResult
from session import Session, Status, activate_turn_fields, clear_turn_fields, now_utc
from turn_context import write_turn_context

logger = get_logger(__name__)


def _turn_fields(session):
    return {
        "active_turn_id": session.active_turn_id,
        "active_turn_event_id": session.active_turn_event_id,
        "active_turn_owner_user_id": session.active_turn_owner_user_id,
        "active_turn_started_at": session.active_turn_started_at,
    }


class InteractiveHandler:
    def __init__(self, tmux_manager, session_store, post_to_slack_fn, state=None):
        self.tmux = tmux_manager
        self.store = session_store
        self.post_to_slack = post_to_slack_fn
        self.state = state

    def on_input_wait_detected(self, session, result: InputWaitResult):
        """InputDetectorから呼ばれるコールバック

        CAS的遷移で busy → waiting_for_input を試み、成功した場合のみSlack通知。
        transition() の updates で input_wait フィールドも原子的に書き込む（2段階操作を排除）。
        """
        updated = self.store.transition(
            session.conversation_identity,
            Status.BUSY,
            Status.WAITING,
            updates={
                **_turn_fields(session),
                "last_activity_at": now_utc(),
                "input_wait": {
                    "type": result.wait_type,
                    "response_format": result.response_format,
                    "options": result.options,
                    "free_text_option": result.free_text_option,
                },
            },
        )
        if updated is None:
            # 既にidle等に遷移済み → 何もしない
            return

        # Slack通知（失敗時はロールバック）
        slack_msg = result.format_for_slack()
        post_result = self.post_to_slack(updated.channel_id, updated.thread_ts, slack_msg)
        ok = post_result.get("ok") if isinstance(post_result, dict) else bool(post_result)
        if not ok:
            self.store.transition(
                session.conversation_identity,
                Status.WAITING,
                Status.BUSY,
                updates={
                    **_turn_fields(session),
                    "input_wait": None,
                    "last_activity_at": now_utc(),
                },
            )
            logger.warning("Slack notify failed, rolled back to busy: %s", session.thread_ts)
            return

        logger.info("Notified Slack for %s: %s", updated.thread_ts, result.wait_type)

    def handle_user_reply(
        self,
        session: Session,
        text: str,
        *,
        event_id: str | None = None,
        owner_user_id: str | None = None,
        payload=None,
    ) -> bool:
        """waiting_for_input状態でユーザーからの回答を受け取り、CC入力に転送

        Returns:
            True: 回答送信成功
            False: 状態不整合等で失敗
        """
        if not session.pane_id:
            logger.error(
                "user reply rejected without pane_id for %s", session.conversation_identity
            )
            return False
        if not session.window_name:
            logger.error(
                "user reply rejected without window_name for %s", session.conversation_identity
            )
            return False
        input_wait = session.input_wait or {}
        updated = self.store.transition(
            session.conversation_identity,
            Status.WAITING,
            Status.BUSY,
            updates={
                **activate_turn_fields(
                    session,
                    turn_id=uuid.uuid4().hex,
                    event_id=event_id,
                    owner_user_id=owner_user_id,
                ),
                "input_wait": None,
            },
        )
        if updated is None:
            return False
        pane_id = updated.pane_id
        window_name = updated.window_name
        if not pane_id or not window_name:
            rolled_back = self.store.transition(
                session.conversation_identity,
                Status.BUSY,
                Status.WAITING,
                updates={
                    **clear_turn_fields(),
                    "last_activity_at": now_utc(),
                    "input_wait": input_wait,
                },
            )
            if rolled_back is None:
                logger.error("Failed to rollback missing pane target: %s", session.thread_ts)
            logger.error(
                "user reply rejected after transition without pane target for %s",
                session.conversation_identity,
            )
            return False
        if self.state is not None and updated.active_turn_id:
            self.state.open_turn_attempt(
                updated.active_turn_id,
                channel_id=updated.channel_id,
                thread_ts=updated.thread_ts,
                event_id=updated.active_turn_event_id or "",
                owner_user_id=updated.active_turn_owner_user_id,
                started_at=updated.active_turn_started_at,
                payload=payload if payload != {} else None,
            )
        target = self.tmux.resolve_target(window_name, pane_id)
        turn_artifacts = None
        if updated.active_turn_id and window_name:
            turn_artifacts = write_turn_context(updated, turn_id=updated.active_turn_id)

        response_format = input_wait.get("response_format", "text")

        answer_steps = self._build_answer_steps(
            text,
            wait_type=input_wait.get("type", ""),
            response_format=response_format,
            free_text_option=input_wait.get("free_text_option"),
        )
        if updated.active_turn_id and answer_steps:
            first_step = f"[turn_id: {updated.active_turn_id}]\n{answer_steps[0]}"
            if turn_artifacts is not None:
                first_step = (
                    f"[turn_id: {updated.active_turn_id}]\n"
                    f"[reply_command: {turn_artifacts.reply_command_path}]\n"
                    f"{answer_steps[0]}"
                )
            answer_steps = [first_step, *answer_steps[1:]]

        try:
            for step in answer_steps:
                self.tmux.send_text_and_enter(target, step)
        except Exception as e:
            rolled_back = self.store.transition(
                session.conversation_identity,
                Status.BUSY,
                Status.WAITING,
                updates={
                    **clear_turn_fields(),
                    "last_activity_at": now_utc(),
                    "input_wait": input_wait,
                },
            )
            if rolled_back is None:
                logger.error("Failed to rollback after tmux error: %s", session.thread_ts)
            logger.error("tmux send failed for %s: %s", session.thread_ts, e)
            return False

        updated.touch()
        self.store.save(updated)

        logger.info(
            "Sent answer to %s: %s", window_name, " / ".join(step[:50] for step in answer_steps)
        )
        return True

    def _build_answer_steps(
        self,
        text: str,
        *,
        wait_type: str,
        response_format: str,
        free_text_option: str | None = None,
    ) -> list[str]:
        """回答テキストをCodex入力形式に変換"""
        text = text.strip()

        if response_format in {"number", "number_or_text"}:
            # "1" or "[1]" の完全一致だけを選択肢番号として扱う。
            match = re.fullmatch(r"\[?(\d+)\]?", text)
            if match:
                return [match.group(1)]
            if wait_type == "ask_user_question" and free_text_option:
                return [free_text_option, text]
            return [text]

        # text → そのまま
        return [text]
