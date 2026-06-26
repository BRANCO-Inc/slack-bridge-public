"""Slack API utilities for Slack Bridge."""

from __future__ import annotations

import math
import mimetypes
import os
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from slack_sdk.errors import SlackApiError

import config
from bridge_logging import get_logger
from config import (
    SLACK_API_MAX_RETRIES,
    SLACK_FILE_DOWNLOAD_MAX_BYTES,
    SLACK_FILE_DOWNLOAD_TIMEOUT_SECONDS,
    SLACK_FILES_DIR,
    SLACK_POST_MAX_LEN,
)
from session import now_utc

if TYPE_CHECKING:
    from bridge_state import BridgeState
    from session_store import SessionStore

logger = get_logger(__name__)
JST = timezone(timedelta(hours=9))
SLACK_PERMANENT_ERRORS = {"missing_scope", "not_in_channel", "not_found"}
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class SlackApiCallFailure(RuntimeError):
    mode: str
    operation: str
    error: str
    status: int | None = None
    retry_after: str | None = None
    classification: str = "permanent"
    attempts: int = 1

    @property
    def diagnostic(self) -> str:
        safe_operation = self.operation.replace(".", "_")
        return f"slack_{self.mode}_{safe_operation}_{self.error}_{self.classification}"

    def __str__(self) -> str:
        details = [self.diagnostic, f"attempts={self.attempts}"]
        if self.status is not None:
            details.append(f"status={self.status}")
        if self.retry_after:
            details.append(f"retry_after={self.retry_after}")
        return " ".join(details)


def _slack_response_error(response) -> str:
    try:
        return response.get("error", "slack_api_error")
    except Exception:
        return "slack_api_error"


def _slack_response_status(response) -> int | None:
    return getattr(response, "status_code", None)


def _slack_response_retry_after(response) -> str | None:
    headers = getattr(response, "headers", {}) or {}
    return headers.get("Retry-After") or headers.get("retry-after")


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            value = float(retry_after)
            if math.isfinite(value) and value >= 0:
                return value
        except ValueError:
            pass
    return min(2 ** max(attempt - 1, 0), 8)


def _classify_slack_error(error: str, status: int | None, *, exhausted: bool) -> str:
    if status == 429 or (status is not None and status >= 500):
        return "retry_exhausted" if exhausted else "transient"
    if error in SLACK_PERMANENT_ERRORS:
        return "permanent"
    return "permanent"


def _call_slack_api_with_retries(mode: str, operation: str, call):
    attempts = max(1, SLACK_API_MAX_RETRIES)
    last_failure: SlackApiCallFailure | None = None
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except SlackApiError as e:
            response = e.response
            error = _slack_response_error(response)
            status = _slack_response_status(response)
            retry_after = _slack_response_retry_after(response)
            retryable = status == 429 or (status is not None and status >= 500)
            exhausted = attempt >= attempts or not retryable
            classification = _classify_slack_error(error, status, exhausted=exhausted)
            last_failure = SlackApiCallFailure(
                mode=mode,
                operation=operation,
                error=error,
                status=status,
                retry_after=retry_after,
                classification=classification,
                attempts=attempt,
            )
            if exhausted:
                raise last_failure from e
            logger.warning(
                "Slack %s %s failed; retrying attempt=%s/%s status=%s error=%s retry_after=%s",
                mode,
                operation,
                attempt,
                attempts,
                status,
                error,
                retry_after,
            )
            time.sleep(_retry_delay(attempt, retry_after))
        except Exception as e:
            raise SlackApiCallFailure(
                mode=mode,
                operation=operation,
                error=type(e).__name__,
                status=None,
                classification="exception",
                attempts=attempt,
            ) from e
    if last_failure is not None:
        raise last_failure
    raise SlackApiCallFailure(
        mode=mode, operation=operation, error="unknown", classification="exception"
    )


def slack_read_call(client, operation: str, **kwargs):
    method = getattr(client, operation)
    return _call_slack_api_with_retries("read", operation, lambda: method(**kwargs))


def split_slack_post_text(text: str, max_len: int = SLACK_POST_MAX_LEN) -> list[str]:
    if max_len <= 0:
        raise ValueError("max_len must be positive")
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        split_at = remaining.rfind("\n", 0, max_len + 1)
        if split_at <= 0:
            split_at = max_len
        else:
            split_at += 1
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining:
        parts.append(remaining)
    return parts


def sanitize_slack_filename(name: str, *, fallback: str = "file") -> str:
    cleaned = CONTROL_CHARS_RE.sub("_", str(name or ""))
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    cleaned = cleaned.replace("..", "_")
    cleaned = cleaned.strip(" ._")
    return cleaned or fallback


def _with_content_type_extension(name: str, content_type: str | None) -> str:
    if os.path.splitext(name)[1] or not content_type:
        return name
    media_type = content_type.split(";", 1)[0].strip().lower()
    extension = mimetypes.guess_extension(media_type)
    if not extension:
        return name
    return f"{name}{extension}"


def _contained_path(base_dir: str, *parts: str) -> str:
    base_real = os.path.realpath(base_dir)
    data_real = os.path.realpath(config.DATA_DIR)
    path = os.path.realpath(os.path.join(base_real, *parts))
    if os.path.commonpath([base_real, path]) != base_real:
        raise ValueError("slack_file_path_outside_files_dir")
    if os.path.commonpath([data_real, path]) != data_real:
        raise ValueError("slack_file_path_outside_data_dir")
    return path


def download_slack_files_with_diagnostics(
    files: list[dict], bot_token: str
) -> tuple[list[str], list[dict]]:
    if not files:
        return [], []
    os.makedirs(SLACK_FILES_DIR, exist_ok=True)
    paths: list[str] = []
    diagnostics: list[dict] = []
    for index, f in enumerate(files):
        url = f.get("url_private")
        raw_name = f.get("name") or f.get("title") or "file"
        file_id = sanitize_slack_filename(f.get("id", ""), fallback=f"file_{index + 1}")
        name = _with_content_type_extension(sanitize_slack_filename(raw_name), f.get("mimetype"))
        diagnostic = {
            "id": f.get("id", ""),
            "name": raw_name,
            "ok": False,
            "path": None,
            "error": None,
        }
        if not url:
            diagnostic["error"] = "missing_url_private"
            diagnostics.append(diagnostic)
            continue
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https":
            diagnostic["error"] = "unsupported_url_scheme"
            diagnostics.append(diagnostic)
            continue
        local_name = f"{file_id}_{name}" if file_id else name
        local_path = None
        try:
            local_path = _contained_path(SLACK_FILES_DIR, local_name)
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bot_token}"})
            with urllib.request.urlopen(req, timeout=SLACK_FILE_DOWNLOAD_TIMEOUT_SECONDS) as resp:
                content_length = resp.headers.get("Content-Length")
                if content_length and int(content_length) > SLACK_FILE_DOWNLOAD_MAX_BYTES:
                    raise ValueError("file_too_large")
                total = 0
                with open(local_path, "wb") as out:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > SLACK_FILE_DOWNLOAD_MAX_BYTES:
                            raise ValueError("file_too_large")
                        out.write(chunk)
            paths.append(local_path)
            diagnostic.update({"ok": True, "path": local_path})
        except Exception as e:
            try:
                if local_path and os.path.exists(local_path):
                    os.remove(local_path)
            except OSError:
                logger.warning("Failed to remove partial Slack file %s", local_path)
            diagnostic["error"] = str(e)
            logger.error("Failed to download Slack file %s: %s", raw_name, e)
        diagnostics.append(diagnostic)
    return paths, diagnostics


def strip_mention(text: str) -> str:
    return re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()


def has_bot_mention(bot_user_id: str, text: str) -> bool:
    return bool(bot_user_id) and f"<@{bot_user_id}>" in text


def _notification_counter_name(channel_id: str, *, now: datetime | None = None) -> str:
    current = now or datetime.now(JST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    local_date = current.astimezone(JST).date().isoformat()
    return f"notification:{channel_id}:{local_date}"


def _claim_notification_slot(state: BridgeState | None, channel_id: str) -> dict:
    if state is None:
        return {"claimed": True}
    if channel_id not in config.NOTIFICATION_RATE_LIMIT_CHANNEL_IDS:
        return {"claimed": True}
    return state.claim_runtime_counter_slot(
        _notification_counter_name(channel_id),
        config.NOTIFICATION_DAILY_LIMIT,
    )


def make_post_to_slack(
    client,
    store: SessionStore,
    state: BridgeState | None = None,
    *,
    conversation_identity_for: Callable[[str, str], str],
):
    def post_to_slack(
        channel_id: str,
        thread_ts: str,
        text: str,
        *,
        substantive: bool = False,
    ) -> dict:
        if not substantive:
            slot = _claim_notification_slot(state, channel_id)
            if not slot.get("claimed"):
                return {
                    "ok": True,
                    "suppressed": True,
                    "reason": "daily_notification_limit",
                    "counter_value": slot.get("counter_value"),
                    "limit": slot.get("limit"),
                    "parts": 0,
                }
        responses = []
        try:
            for part in split_slack_post_text(text):
                response = _call_slack_api_with_retries(
                    "post",
                    "chat_postMessage",
                    lambda part=part: client.chat_postMessage(
                        channel=channel_id,
                        text=part,
                        thread_ts=thread_ts,
                    ),
                )
                responses.append(response)
        except SlackApiCallFailure as e:
            result = {
                "ok": False,
                "error": e.error,
                "diagnostic": e.diagnostic,
                "status": e.status,
                "retry_after": e.retry_after,
                "posted_reply_ts": [response.get("ts") for response in responses],
            }
            return result
        except Exception as e:
            result = {"ok": False, "error": str(e), "status": 500}
            return result

        response = responses[-1] if responses else {}
        session = store.load(conversation_identity_for(channel_id, thread_ts))
        if session:
            updates: dict[str, object] = {"last_activity_at": now_utc()}
            if substantive:
                updates.update(
                    {
                        "last_reply_ts": response.get("ts"),
                        "last_substantive_reply_ts": response.get("ts"),
                        "last_thread_message_ts": response.get("ts"),
                    }
                )
            updated = store.update_fields(session.conversation_identity, updates)
            if updated is None:
                session.touch()
                if substantive:
                    session.last_reply_ts = response.get("ts")
                    session.last_substantive_reply_ts = response.get("ts")
                    session.last_thread_message_ts = response.get("ts")
                store.save(session)
        return {
            "ok": True,
            "reply_ts": response.get("ts"),
            "reply_ts_list": [posted.get("ts") for posted in responses],
            "parts": len(responses),
        }

    return post_to_slack


def make_add_reaction(client):
    def add_reaction(channel_id: str, message_ts: str, name: str) -> dict:
        try:
            response = _call_slack_api_with_retries(
                "post",
                "reactions_add",
                lambda: client.reactions_add(
                    channel=channel_id,
                    timestamp=message_ts,
                    name=name,
                ),
            )
        except SlackApiCallFailure as e:
            if e.error == "already_reacted":
                return {"ok": True, "already_reacted": True}
            return {
                "ok": False,
                "error": e.error,
                "diagnostic": e.diagnostic,
                "status": e.status,
                "retry_after": e.retry_after,
            }
        except Exception as e:
            return {"ok": False, "error": str(e), "status": 500}

        if isinstance(response, dict) and response.get("ok", True) is False:
            return {"ok": False, "error": response.get("error", "slack_reaction_failed")}
        return {"ok": True}

    return add_reaction
