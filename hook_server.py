"""Turn completion / case reply hook server."""

from __future__ import annotations

import hmac
import json
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import config
from bridge_logging import get_logger
from bridge_state import BridgeState
from config import HOOK_SERVER_PORT
from extensions.registry import (
    is_optional_extension_analysis_report_reply,
    record_optional_extension_analysis_report_post_failure,
)
from pane_pool import canonical_window_name
from session import Status, now_utc
from turn_context import cleanup_turn_context

logger = get_logger(__name__)

CASE_REPLY_PATH = "/bridge/case_reply"
CASE_REACTION_PATH = "/bridge/case_reaction"
LATE_COMPLETION_WINDOW_SECONDS = getattr(config, "LATE_COMPLETION_WINDOW_SECONDS", 60)
DISCARD_NOTICE_SUFFIX = "_discard_notice"
CASE_REPLY_POSTABLE_STATUS_VALUES = {Status.BUSY.value, Status.WAITING.value}
MAX_REQUEST_BODY_BYTES = int(getattr(config, "HOOK_MAX_REQUEST_BODY_BYTES", 1024 * 1024))


class RequestBodyTooLarge(Exception):
    pass


@dataclass
class HookBridge:
    store: Any
    state: BridgeState
    post_to_slack: Any
    handle_turn_complete: Any
    add_reaction: Any = None


class HookHTTPServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._inflight = 0
        self._inflight_condition = threading.Condition()

    def begin_request(self):
        with self._inflight_condition:
            self._inflight += 1

    def end_request(self):
        with self._inflight_condition:
            self._inflight -= 1
            self._inflight_condition.notify_all()

    def wait_for_inflight(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._inflight_condition:
            while self._inflight:
                if deadline is None:
                    self._inflight_condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._inflight_condition.wait(remaining)
            return True


class HookHandler(BaseHTTPRequestHandler):
    bridge: ClassVar[HookBridge]

    def log_message(self, format, *args):
        pass

    def _json(self, code, payload, headers: dict[str, str] | None = None):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        token = (config.SLACK_BRIDGE_AUTH_TOKEN or "").strip()
        if not token:
            logger.error("SLACK_BRIDGE_AUTH_TOKEN is not configured; rejecting hook request")
            return False
        supplied = self.headers.get("X-Bridge-Token", "")
        return hmac.compare_digest(supplied, token)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length < 0:
                return None
            if length > MAX_REQUEST_BODY_BYTES:
                raise RequestBodyTooLarge
            return json.loads(self.rfile.read(length)) if length > 0 else {}
        except json.JSONDecodeError, ValueError:
            return None

    def do_POST(self):
        tracker = getattr(self.server, "begin_request", None)
        if callable(tracker):
            tracker()
        try:
            if self.path not in (CASE_REPLY_PATH, CASE_REACTION_PATH):
                self._json(404, {"ok": False, "error": "not_found"})
                return
            if not self._authorized():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                body = self._read_json()
            except RequestBodyTooLarge:
                self._json(413, {"ok": False, "error": "request_body_too_large"})
                return
            if body is None:
                self._json(400, {"ok": False, "error": "invalid_json"})
                return
            if self.path == CASE_REACTION_PATH:
                self._handle_case_reaction(body)
                return
            self._handle_case_reply(body)
        except Exception:
            logger.exception("Unhandled hook request error path=%s", self.path)
            self._json(500, {"ok": False, "error": "internal_server_error"})
        finally:
            tracker = getattr(self.server, "end_request", None)
            if callable(tracker):
                tracker()

    def _resolve_session(self, *, case_id: str, session_id: str | None = None):
        session = None
        if session_id:
            session = self.bridge.store.find_by_field("worker_session_id", session_id)
        if session is None:
            session = self.bridge.store.find_by_field("case_id", case_id)
        return session

    def _normalize_post_result(self, result):
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            return {"ok": True, "reply_ts": result}
        return {"ok": bool(result)}

    def _update_session_fields(self, session, updates: dict):
        updater = getattr(self.bridge.store, "update_fields", None)
        if callable(updater):
            updated = updater(session.conversation_identity, updates)
            if updated is not None:
                return updated
        for key, value in updates.items():
            setattr(session, key, value)
        saver = getattr(self.bridge.store, "save", None)
        if callable(saver):
            with suppress(Exception):
                saver(session)
        return session

    def _validate_callback_identity(
        self,
        session,
        *,
        session_id: str | None = None,
        pane_id: str | None = None,
        window_name: str | None = None,
    ):
        if (
            session_id
            and getattr(session, "worker_session_id", None)
            and session.worker_session_id != session_id
        ):
            return "stale_session"
        if pane_id and getattr(session, "pane_id", None) != pane_id:
            return "stale_pane"
        if window_name and canonical_window_name(getattr(session, "window_name", None)) != (
            canonical_window_name(window_name)
        ):
            return "stale_window"
        return None

    def _validate_turn_attempt_session(self, session, turn_attempt: dict):
        channel_id = turn_attempt.get("channel_id")
        thread_ts = turn_attempt.get("thread_ts")
        if channel_id and channel_id != getattr(session, "channel_id", None):
            return "stale_turn"
        if thread_ts and thread_ts != getattr(session, "thread_ts", None):
            return "stale_turn"
        return None

    def _validate_case_reply_ownership(self, session, *, turn_id: str, turn_attempt: dict):
        session_error = self._validate_turn_attempt_session(session, turn_attempt)
        if session_error:
            return session_error
        if turn_attempt.get("completion_action") == "cancelled":
            return None
        if turn_attempt.get("completed_at"):
            return "stale_turn"
        status_value = getattr(
            getattr(session, "status", None), "value", getattr(session, "status", None)
        )
        if status_value not in CASE_REPLY_POSTABLE_STATUS_VALUES:
            return "stale_turn"
        if getattr(session, "active_turn_id", None) != turn_id:
            return "stale_turn"
        return None

    def _reload_session(self, session):
        loader = getattr(self.bridge.store, "load", None)
        if not callable(loader):
            return session
        try:
            return loader(session.conversation_identity) or session
        except Exception:
            return session

    def _turn_attempt_started_within_window(
        self, turn_attempt: dict, *, now: datetime | None = None
    ) -> bool:
        started_at = turn_attempt.get("started_at")
        if not started_at:
            return True
        try:
            started = datetime.fromisoformat(started_at)
        except ValueError:
            return True
        current = now or datetime.now(started.tzinfo or UTC)
        if started.tzinfo is None:
            started = started.replace(tzinfo=current.tzinfo)
        return current - started <= timedelta(seconds=LATE_COMPLETION_WINDOW_SECONDS)

    def _post_reply(
        self,
        channel_id: str,
        thread_ts: str,
        text: str,
        *,
        substantive: bool,
        reply_request_id: str | None = None,
    ):
        delay = 1
        last_result = {"ok": False, "error": "slack_post_failed"}
        set_reply_request_id = getattr(self.bridge.post_to_slack, "set_reply_request_id", None)
        reset_reply_request_id = getattr(self.bridge.post_to_slack, "reset_reply_request_id", None)
        if callable(set_reply_request_id) and callable(reset_reply_request_id):
            token = set_reply_request_id(reply_request_id)
            reset_reply_request_context = reset_reply_request_id
        else:
            token = None
            reset_reply_request_context = None
        try:
            for attempt in range(3):
                last_result = self._normalize_post_result(
                    self.bridge.post_to_slack(
                        channel_id,
                        thread_ts,
                        text,
                        substantive=substantive,
                    )
                )
                if last_result.get("ok"):
                    return last_result
                if last_result.get("posted_reply_ts"):
                    return last_result
                if attempt < 2:
                    retry_after = last_result.get("retry_after")
                    try:
                        sleep_seconds = int(retry_after) if retry_after is not None else delay
                    except TypeError, ValueError:
                        sleep_seconds = delay
                    if not config.TEST_MODE:
                        time.sleep(max(1, sleep_seconds))
                    delay *= 2
        finally:
            if reset_reply_request_context is not None:
                reset_reply_request_context(token)
        logger.error(
            "Slack post failed after retries channel=%s thread=%s error=%s",
            channel_id,
            thread_ts,
            last_result.get("error", "slack_post_failed"),
        )
        return last_result

    def _post_reaction(self, channel_id: str, message_ts: str, reaction_name: str):
        add_reaction = getattr(self.bridge, "add_reaction", None)
        if not callable(add_reaction):
            return {"ok": False, "error": "reaction_not_configured"}
        delay = 1
        last_result = {"ok": False, "error": "slack_reaction_failed"}
        for attempt in range(3):
            last_result = self._normalize_post_result(
                add_reaction(channel_id, message_ts, reaction_name)
            )
            if last_result.get("ok"):
                return last_result
            if attempt < 2:
                retry_after = last_result.get("retry_after")
                try:
                    sleep_seconds = int(retry_after) if retry_after is not None else delay
                except TypeError, ValueError:
                    sleep_seconds = delay
                if not config.TEST_MODE:
                    time.sleep(max(1, sleep_seconds))
                delay *= 2
        logger.error(
            "Slack reaction failed after retries channel=%s ts=%s reaction=%s error=%s",
            channel_id,
            message_ts,
            reaction_name,
            last_result.get("error", "slack_reaction_failed"),
        )
        return last_result

    def _mark_turn_attempt(self, turn_id: str, **updates):
        return self.bridge.state.mark_turn_attempt_completed(turn_id, **updates)

    def _release_turn_reply_claim(self, turn_id: str, *, reply_request_id: str) -> None:
        self.bridge.state.release_turn_reply_claim(turn_id, reply_request_id=reply_request_id)

    def _post_discard_notice(self, session, *, reply_request_id: str, turn_id: str, text: str):
        discard_reply_request_id = f"{turn_id}{DISCARD_NOTICE_SUFFIX}"
        claim = self.bridge.state.claim_reply(
            discard_reply_request_id,
            case_id=session.case_id,
            payload={
                "case_id": session.case_id,
                "turn_id": turn_id,
                "reply_request_id": reply_request_id,
                "text": text,
                "kind": "discard_notice",
            },
        )
        if claim.conflict:
            return {"ok": False, "error": "discard_notice_conflict"}
        if not claim.claimed:
            row = claim.row or self.bridge.state.get_reply(discard_reply_request_id) or {}
            if row.get("status") == "posted":
                return {"ok": True, "deduped": True, "reply_ts": row.get("reply_ts", "")}
            if row.get("status") == "received":
                return {
                    "ok": False,
                    "error": "discard_notice_in_progress",
                    "status": 503,
                    "retry_after": 1,
                }

        result = self._post_reply(
            session.channel_id,
            session.thread_ts,
            "⚠️ キャンセル後に生成された回答は投稿しませんでした。\n確認用に内容を表示します。\n\n--- 生成済み回答 ---\n"
            + text,
            substantive=True,
        )
        if not result.get("ok"):
            error = result.get("error", "slack_post_failed")
            self.bridge.state.mark_reply_failed(discard_reply_request_id, last_error=error)
            return result

        reply_ts = result.get("reply_ts") or ""
        self.bridge.state.mark_reply_posted(
            discard_reply_request_id,
            channel_id=session.channel_id,
            thread_ts=session.thread_ts,
            reply_ts=reply_ts,
        )
        if hasattr(self.bridge.state, "mark_turn_attempt_discard_notified"):
            try:
                self.bridge.state.mark_turn_attempt_discard_notified(turn_id)
            except Exception:
                logger.debug(
                    "discard_notified update failed for turn_id=%s", turn_id, exc_info=True
                )
        return {"ok": True, "reply_ts": reply_ts, "status": "discarded"}

    def _handle_case_reaction(self, body: dict):
        reaction_request_id = body.get("reaction_request_id", "").strip()
        case_id = body.get("case_id", "").strip()
        turn_id = body.get("turn_id", "").strip()
        window_name = body.get("window_name", "").strip()
        pane_id = body.get("pane_id", "").strip()
        session_id = body.get("session_id", "").strip()
        reaction_name = body.get("reaction_name", "").strip()
        if not reaction_request_id:
            self._json(400, {"ok": False, "error": "reaction_request_id_required"})
            return
        if not turn_id:
            self._json(400, {"ok": False, "error": "turn_id_required"})
            return
        if not case_id:
            self._json(400, {"ok": False, "error": "case_id_required"})
            return
        if reaction_name != "white_check_mark":
            self._json(400, {"ok": False, "error": "unsupported_reaction"})
            return
        if not session_id or not pane_id or not window_name:
            self._json(400, {"ok": False, "error": "callback_identity_required"})
            return

        session = self._resolve_session(case_id=case_id, session_id=session_id)
        if not session:
            self._json(404, {"ok": False, "error": "case_not_found"})
            return
        identity_error = self._validate_callback_identity(
            session,
            session_id=session_id,
            pane_id=pane_id,
            window_name=window_name,
        )
        if identity_error:
            self._json(409, {"ok": False, "error": identity_error})
            return
        if session.status in {Status.ERROR, Status.KILLED}:
            self._json(409, {"ok": False, "error": "session_terminal"})
            return

        turn_attempt = self.bridge.state.get_turn_attempt(turn_id)
        if turn_attempt is None:
            self._json(409, {"ok": False, "error": "stale_turn"})
            return
        ownership_error = self._validate_case_reply_ownership(
            session, turn_id=turn_id, turn_attempt=turn_attempt
        )
        if ownership_error:
            cleanup_turn_context(turn_id)
            self._json(409, {"ok": False, "error": ownership_error})
            return

        result = self._post_reaction(session.channel_id, session.thread_ts, reaction_name)
        if not result.get("ok"):
            error = result.get("error", "slack_reaction_failed")
            if result.get("status") == 429:
                retry_after = str(result.get("retry_after") or "1")
                self._json(429, {"ok": False, "error": error}, headers={"Retry-After": retry_after})
                return
            self._json(503, {"ok": False, "error": error})
            return

        self._update_session_fields(
            session,
            {
                "last_activity_at": now_utc(),
                "worker_session_id": session_id or session.worker_session_id,
            },
        )
        self._mark_turn_attempt(
            turn_id,
            completed_at=now_utc(),
            completion_action="reaction",
        )
        completion = self.bridge.handle_turn_complete(
            case_id=case_id,
            window_name=window_name,
            session_id=session_id,
            pane_id=pane_id,
            turn_id=turn_id,
            action="done",
        )
        cleanup_turn_context(turn_id)
        payload = {
            "ok": True,
            "reaction_name": reaction_name,
            "message_ts": session.thread_ts,
            "status": completion or "done",
        }
        if result.get("already_reacted"):
            payload["already_reacted"] = True
        self._json(200, payload)

    def _handle_case_reply(self, body: dict):
        reply_request_id = body.get("reply_request_id", "").strip()
        case_id = body.get("case_id", "").strip()
        turn_id = body.get("turn_id", "").strip()
        window_name = body.get("window_name", "").strip()
        pane_id = body.get("pane_id", "").strip()
        session_id = body.get("session_id", "").strip()
        text = body.get("text", "")
        completion_action = (body.get("completion_action") or "done").strip() or "done"
        final_attempt = body.get("final_attempt") is True
        if not reply_request_id:
            self._json(400, {"ok": False, "error": "reply_request_id_required"})
            return
        if not turn_id:
            self._json(400, {"ok": False, "error": "turn_id_required"})
            return
        if not case_id or not isinstance(text, str) or not text.strip():
            self._json(400, {"ok": False, "error": "case_id_and_text_required"})
            return
        if not session_id or not pane_id or not window_name:
            self._json(400, {"ok": False, "error": "callback_identity_required"})
            return
        if completion_action not in {"done", "wait", "continue", "close", "error"}:
            self._json(400, {"ok": False, "error": "unsupported_completion_action"})
            return

        payload = {
            "case_id": case_id,
            "turn_id": turn_id,
            "text": text,
            "completion_action": completion_action,
            "session_id": session_id,
            "window_name": window_name,
            "pane_id": pane_id,
        }
        claim = self.bridge.state.claim_reply(reply_request_id, case_id=case_id, payload=payload)
        if claim.conflict:
            self._json(409, {"ok": False, "error": "reply_request_id_conflict"})
            return
        if not claim.claimed:
            row = claim.row or self.bridge.state.get_reply(reply_request_id) or {}
            status = row.get("status", "")
            if status in {"posted", "posted_unknown"}:
                self._json(200, {"ok": True, "reply_ts": row.get("reply_ts"), "deduped": True})
                return
            if status == "received":
                self._json(
                    503,
                    {"ok": False, "error": "reply_request_in_progress"},
                    headers={"Retry-After": "1"},
                )
                return
            self._json(409, {"ok": False, "error": "reply_request_not_claimed", "status": status})
            return

        session = self._resolve_session(case_id=case_id, session_id=session_id)
        if not session:
            self.bridge.state.mark_reply_failed(reply_request_id, last_error="case_not_found")
            self._json(404, {"ok": False, "error": "case_not_found"})
            return
        identity_error = self._validate_callback_identity(
            session,
            session_id=session_id,
            pane_id=pane_id,
            window_name=window_name,
        )
        if identity_error:
            self.bridge.state.mark_reply_failed(reply_request_id, last_error=identity_error)
            self._json(409, {"ok": False, "error": identity_error})
            return
        if session.status in {Status.ERROR, Status.KILLED}:
            self.bridge.state.mark_reply_failed(reply_request_id, last_error="session_terminal")
            self._json(409, {"ok": False, "error": "session_terminal"})
            return

        turn_attempt = self.bridge.state.get_turn_attempt(turn_id)
        if turn_attempt is None:
            self.bridge.state.mark_reply_failed(reply_request_id, last_error="stale_turn")
            self._json(409, {"ok": False, "error": "stale_turn"})
            return

        ownership_error = self._validate_case_reply_ownership(
            session, turn_id=turn_id, turn_attempt=turn_attempt
        )
        if ownership_error:
            cleanup_turn_context(turn_id)
            self.bridge.state.mark_reply_failed(reply_request_id, last_error=ownership_error)
            self._json(409, {"ok": False, "error": ownership_error})
            return

        if turn_attempt.get("completion_action") == "cancelled":
            if turn_attempt.get("discard_notified_at"):
                cleanup_turn_context(turn_id)
                self.bridge.state.mark_reply_failed(
                    reply_request_id, last_error="discarded_cancelled"
                )
                self._json(200, {"ok": True, "status": "discarded", "deduped": True})
                return
            discard_result = self._post_discard_notice(
                session,
                reply_request_id=reply_request_id,
                turn_id=turn_id,
                text=text,
            )
            if not discard_result.get("ok"):
                error = discard_result.get("error", "slack_post_failed")
                self.bridge.state.mark_reply_failed(reply_request_id, last_error=error)
                if discard_result.get("status") == 429:
                    retry_after = str(discard_result.get("retry_after") or "1")
                    self._json(
                        429, {"ok": False, "error": error}, headers={"Retry-After": retry_after}
                    )
                    return
                self._json(503, {"ok": False, "error": error})
                return
            cleanup_turn_context(turn_id)
            self.bridge.state.mark_reply_failed(reply_request_id, last_error="discarded_cancelled")
            self._json(200, discard_result)
            return

        session = self._reload_session(session)
        identity_error = self._validate_callback_identity(
            session,
            session_id=session_id,
            pane_id=pane_id,
            window_name=window_name,
        )
        if identity_error:
            cleanup_turn_context(turn_id)
            self.bridge.state.mark_reply_failed(reply_request_id, last_error=identity_error)
            self._json(409, {"ok": False, "error": identity_error})
            return
        ownership_error = self._validate_case_reply_ownership(
            session, turn_id=turn_id, turn_attempt=turn_attempt
        )
        if ownership_error:
            cleanup_turn_context(turn_id)
            self.bridge.state.mark_reply_failed(reply_request_id, last_error=ownership_error)
            self._json(409, {"ok": False, "error": ownership_error})
            return

        turn_claim = self.bridge.state.claim_turn_reply(turn_id, reply_request_id=reply_request_id)
        if not turn_claim.claimed:
            cleanup_turn_context(turn_id)
            self.bridge.state.mark_reply_failed(reply_request_id, last_error="stale_turn")
            self._json(409, {"ok": False, "error": "stale_turn"})
            return
        turn_attempt = turn_claim.row or turn_attempt

        substantive = completion_action != "continue"
        result = self._post_reply(
            session.channel_id,
            session.thread_ts,
            text,
            substantive=substantive,
            reply_request_id=reply_request_id,
        )
        posted_status = "posted"
        posted_error = ""
        if not result.get("ok"):
            error = result.get("error", "slack_post_failed")
            posted_reply_ts = [ts for ts in result.get("posted_reply_ts") or [] if ts]
            if posted_reply_ts:
                reply_ts = posted_reply_ts[-1]
                try:
                    self.bridge.state.mark_reply_posted_unknown(
                        reply_request_id,
                        channel_id=session.channel_id,
                        thread_ts=session.thread_ts,
                        reply_ts=reply_ts,
                        last_error=error,
                    )
                except Exception:
                    logger.exception(
                        "Slack reply partially posted but posted_unknown update failed: reply_request_id=%s reply_ts=%s",
                        reply_request_id,
                        reply_ts,
                    )
                posted_status = "posted_unknown"
                posted_error = error
            else:
                self._release_turn_reply_claim(turn_id, reply_request_id=reply_request_id)
                self.bridge.state.mark_reply_failed(reply_request_id, last_error=error)
                if final_attempt and is_optional_extension_analysis_report_reply(text):
                    failure_record = record_optional_extension_analysis_report_post_failure(
                        case_id=case_id,
                        reply_request_id=reply_request_id,
                        turn_id=turn_id,
                        error=error,
                        text=text,
                    )
                    if not failure_record.get("ok") and not failure_record.get("skipped"):
                        logger.warning(
                            "optional_extension analysis post failure recorder failed: reply_request_id=%s error=%s detail=%s",
                            reply_request_id,
                            failure_record.get("error"),
                            failure_record,
                        )
                if result.get("status") == 429:
                    retry_after = str(result.get("retry_after") or "1")
                    self._json(
                        429, {"ok": False, "error": error}, headers={"Retry-After": retry_after}
                    )
                    return
                self._json(503, {"ok": False, "error": error})
                return
        else:
            reply_ts = result.get("reply_ts") or ""
            try:
                self.bridge.state.mark_reply_posted(
                    reply_request_id,
                    channel_id=session.channel_id,
                    thread_ts=session.thread_ts,
                    reply_ts=reply_ts,
                )
            except Exception as error:
                posted_status = "posted_state_unknown"
                posted_error = str(error)
                logger.exception(
                    "Slack reply posted but reply ledger update failed: reply_request_id=%s reply_ts=%s",
                    reply_request_id,
                    reply_ts,
                )
                try:
                    self.bridge.state.mark_reply_posted_unknown(
                        reply_request_id,
                        channel_id=session.channel_id,
                        thread_ts=session.thread_ts,
                        reply_ts=reply_ts,
                        last_error=posted_error,
                    )
                except Exception:
                    logger.exception(
                        "Slack reply posted but posted_unknown fallback failed: reply_request_id=%s reply_ts=%s",
                        reply_request_id,
                        reply_ts,
                    )
        self._update_session_fields(
            session,
            {
                "last_activity_at": now_utc(),
                "last_reply_request_id": reply_request_id,
                "last_reply_ts": reply_ts,
                "worker_session_id": session_id or session.worker_session_id,
                **(
                    {
                        "last_substantive_reply_ts": reply_ts,
                        "last_thread_message_ts": reply_ts,
                    }
                    if substantive
                    else {}
                ),
            },
        )

        if turn_attempt.get("completed_at"):
            self._mark_turn_attempt(turn_id, reply_accepted_count_delta=1)
            cleanup_turn_context(turn_id)
            payload = {"ok": True, "reply_ts": reply_ts, "status": posted_status}
            if posted_error:
                payload["error"] = posted_error
            self._json(202 if posted_error else 200, payload)
            return

        if completion_action == "continue":
            self._mark_turn_attempt(
                turn_id,
                reply_accepted_count_delta=1,
                completed_at=None,
            )
            self._release_turn_reply_claim(turn_id, reply_request_id=reply_request_id)
            payload = {
                "ok": True,
                "reply_ts": reply_ts,
                "status": posted_status if posted_error else "continue",
            }
            if posted_error:
                payload["error"] = posted_error
            self._json(202 if posted_error else 200, payload)
            return

        final_action = completion_action
        if completion_action in {"done", "wait", "close"}:
            final_action = "close"
        self._mark_turn_attempt(
            turn_id,
            reply_accepted_count_delta=1,
            completed_at=now_utc(),
            completion_action=final_action,
        )
        completion = self.bridge.handle_turn_complete(
            case_id=case_id,
            window_name=window_name,
            session_id=session_id,
            pane_id=pane_id,
            turn_id=turn_id,
            action=completion_action,
        )
        if completion in {
            "not_found",
            "stale_session",
            "stale_pane",
            "stale_window",
            "stale_turn",
            "terminal",
        }:
            logger.warning(
                "case_reply completion raced after post: case_id=%s turn_id=%s completion=%s reply_ts=%s",
                case_id,
                turn_id,
                completion,
                reply_ts,
            )
            cleanup_turn_context(turn_id)
            payload = {
                "ok": True,
                "reply_ts": reply_ts,
                "status": posted_status,
                "completion": completion,
            }
            if posted_error:
                payload["error"] = posted_error
            self._json(202 if posted_error else 200, payload)
            return
        cleanup_turn_context(turn_id)
        payload = {
            "ok": True,
            "reply_ts": reply_ts,
            "status": posted_status if posted_error else completion or completion_action,
        }
        if posted_error:
            payload["error"] = posted_error
        self._json(202 if posted_error else 200, payload)
        return


def start_hook_server(bridge: HookBridge) -> HookHTTPServer:
    if not (config.SLACK_BRIDGE_AUTH_TOKEN or "").strip():
        raise RuntimeError("SLACK_BRIDGE_AUTH_TOKEN must be set before starting the hook server")
    HookHandler.bridge = bridge
    server = HookHTTPServer(("127.0.0.1", HOOK_SERVER_PORT), HookHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Hook server listening on http://127.0.0.1:%s", HOOK_SERVER_PORT)
    return server
