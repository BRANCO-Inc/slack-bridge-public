"""Slack Bridge."""

from __future__ import annotations

import os
import re
import signal
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

import config
import envelope as envelope_builder
import slack_api
from bridge_logging import get_logger
from bridge_state import BridgeState
from config import (
    DEFAULT_ACK_TEXT,
    GENERAL_WINDOW_NAME,
    MAX_CONCURRENT_SESSIONS,
    RECOVERY_PROGRESS_TEXT,
    RESUME_FAILED_TEXT,
    SESSION_INTERRUPTED_TEXT,
    SESSION_KILLED_TEXT,
    SLACK_BOT_TOKEN,
    SLACK_USER_TOKEN,
    TMUX_SESSION_NAME,
)
from config import (
    OPTIONAL_EXTENSION_WINDOW_NAME as CONFIG_OPTIONAL_EXTENSION_WINDOW_NAME,
)
from extensions import EXTENSIONS
from extensions.base import handle_envelope_extensions, make_outbound_pipeline
from extensions.registry import (
    OPTIONAL_EXTENSION_POOL_WAITING_REASON_PREFIX,
    is_duplicate_optional_extension_notification,
    is_optional_extension_notification_message,
    is_optional_extension_notification_metadata,
    is_optional_extension_pool_mismatch,
    mark_duplicate_optional_extension_notification,
    optional_extension_ack_text,
)
from hook_server import HookBridge, start_hook_server
from input_detector import InputDetector, detect_session_control_command
from interactive_handler import InteractiveHandler
from message_dispatch import MessageDispatch
from pane_pool import (
    all_known_window_names,
    canonical_window_name,
    capacity_for_pool,
    known_windows_for_pool,
    pool_for_envelope,
    pool_for_session,
    windows_for_pool,
)
from session import Session, Status, clear_turn_fields, now_utc
from session_lifecycle import SessionLifecycle, reap_idle_sessions
from session_store import SessionStore
from slack_copy.renderer import render_message
from tmux_gateway import TmuxGateway
from turn_context import cleanup_turn_context
from worker_process import WorkerProcess

logger = get_logger(__name__)

app: App | None = None
user_client: WebClient | None = None
SESSION_CANCEL_ACTION_IDS = {"session_cancel", "slack_bridge_cancel", "cancel_session"}
SESSION_END_ACTION_IDS = {"session_end", "slack_bridge_end", "end_session"}
GRACEFUL_SHUTDOWN_REASON = "graceful_shutdown_restart"
OPTIONAL_EXTENSION_WINDOW_NAME = CONFIG_OPTIONAL_EXTENSION_WINDOW_NAME
ACTIVE_CAPACITY_STATUSES = {
    Status.CREATING,
    Status.STARTING,
    Status.READY,
    Status.BUSY,
    Status.IDLE,
    Status.WAITING,
}
RECOVERABLE_READY_TIMEOUT_REASONS = frozenset(
    {
        "launch_failed: ready_timeout",
        "launch_failed: ready_timeout_after_codex_update",
    }
)


def _tokens_available_for_import_init() -> bool:
    if config.SLACK_BRIDGE_PROFILE in config.PUBLIC_PROFILES:
        return bool(SLACK_BOT_TOKEN)
    return bool(SLACK_BOT_TOKEN and SLACK_USER_TOKEN)


def _initialize_clients() -> None:
    global SLACK_APP_TOKEN, SLACK_BOT_TOKEN, SLACK_USER_TOKEN, app, user_client

    SLACK_BOT_TOKEN = config.SLACK_BOT_TOKEN
    SLACK_APP_TOKEN = config.SLACK_APP_TOKEN
    SLACK_USER_TOKEN = config.SLACK_USER_TOKEN
    app = App(
        token=SLACK_BOT_TOKEN,
        ignoring_self_events_enabled=False,
        token_verification_enabled=not config.TEST_MODE,
    )
    user_client = WebClient(token=SLACK_USER_TOKEN or SLACK_BOT_TOKEN)


def _ensure_clients() -> tuple[App, WebClient]:
    if app is None or user_client is None:
        config.resolve_runtime_secrets()
        config.resolve_runtime_paths()
        _initialize_clients()
    assert app is not None
    assert user_client is not None
    return app, user_client


if _tokens_available_for_import_init():
    _initialize_clients()


def _copy_context(**values: object) -> dict[str, object]:
    context: dict[str, object] = {}
    for key, value in values.items():
        normalized = "" if value is None else str(value).strip()
        context[key] = normalized or "未記載"
    return context


@dataclass
class BridgeRuntime:
    store: SessionStore
    state: BridgeState
    worker: WorkerProcess
    dispatch: MessageDispatch
    lifecycle: SessionLifecycle
    post_to_slack: Callable[..., dict]
    bot_user_id: str
    app_client: WebClient
    user_client: WebClient
    add_reaction: Callable[..., dict] | None = None


def primary_window_for_pool(pool_name: str) -> str:
    windows = windows_for_pool(pool_name)
    return windows[0] if windows else GENERAL_WINDOW_NAME


def make_say(runtime: BridgeRuntime, channel_id: str, thread_ts: str):
    default_thread_ts = thread_ts

    def say(
        text: str,
        thread_ts_override: str | None = None,
        *,
        thread_ts: str | None = None,
    ):
        runtime.post_to_slack(
            channel_id, thread_ts_override or thread_ts or default_thread_ts, text
        )

    return say


def is_recoverable_ready_timeout_error(session: Session | None) -> bool:
    return bool(
        session
        and session.status is Status.ERROR
        and session.failure_reason in RECOVERABLE_READY_TIMEOUT_REASONS
    )


def revive_recoverable_ready_timeout_session(
    runtime: BridgeRuntime, session: Session
) -> Session | None:
    return runtime.store.update_fields(
        session.conversation_identity,
        {
            "status": Status.SUSPENDED,
            "pane_id": None,
            "input_wait": None,
            "last_activity_at": now_utc(),
            **clear_turn_fields(),
        },
    )


def claim_and_mark_event_done(
    runtime: BridgeRuntime,
    body: dict,
    event: dict,
    *,
    failure_reason: str,
    conversation_identity: str | None = None,
    source_event_type: str | None = None,
) -> None:
    event_id = body.get("event_id", "")
    if not event_id:
        return
    if not hasattr(runtime, "state"):
        return
    claim = runtime.state.claim_event(
        event_id,
        envelope_id=body.get("envelope_id", ""),
        source_event_type=source_event_type or event.get("type", ""),
        conversation_identity=conversation_identity,
    )
    if not claim.claimed:
        return
    runtime.state.update_event(
        event_id,
        status="done",
        conversation_identity=conversation_identity,
        failure_reason=failure_reason,
    )


def maybe_post_fixed_ack(
    runtime: BridgeRuntime, event_envelope: dict, session: Session | None
) -> None:
    if not envelope_builder.is_allowed_slack_connect_channel(event_envelope.get("channel", {})):
        if runtime.state.claim_ack(event_envelope["event_id"]):
            runtime.state.update_ack(
                event_envelope["event_id"], status="suppressed_external_shared"
            )
        return
    if event_envelope["normalized_event_type"] not in {
        "new_message",
        "thread_message",
        "direct_message",
    }:
        return
    if session and session.status not in {Status.READY, Status.IDLE}:
        if runtime.state.claim_ack(event_envelope["event_id"]):
            runtime.state.update_ack(event_envelope["event_id"], status="suppressed_busy")
        return
    if not runtime.state.claim_ack(event_envelope["event_id"]):
        return
    ack_text = optional_extension_ack_text(event_envelope) or DEFAULT_ACK_TEXT
    result = runtime.post_to_slack(
        event_envelope["reply_target"]["channel_id"],
        event_envelope["reply_target"]["thread_ts"],
        ack_text,
    )
    if result.get("suppressed") and result.get("reason") == "daily_notification_limit":
        runtime.state.update_ack(event_envelope["event_id"], status="suppressed_daily_limit")
        return
    if result.get("ok"):
        runtime.state.update_ack(
            event_envelope["event_id"], status="posted", reply_ts=result.get("reply_ts")
        )
        return
    runtime.state.update_ack(
        event_envelope["event_id"],
        status="suppressed_ratelimit" if result.get("status") == 429 else "timed_out",
    )


RECOVERABLE_TMUX_STATUSES = {
    Status.CREATING,
    Status.STARTING,
    Status.READY,
    Status.BUSY,
    Status.IDLE,
    Status.WAITING,
    Status.SUSPENDED,
}


def reap_orphan_panes(
    store,
    tmux,
    *,
    window_name: str = GENERAL_WINDOW_NAME,
    sessions: list[Session] | None = None,
    live_panes: list[str] | None = None,
    log_context: str = "orphan pane reaper",
) -> list[str]:
    if sessions is None:
        sessions = [
            session for session in store.list_all() if session.status in RECOVERABLE_TMUX_STATUSES
        ]
    if live_panes is None:
        live_panes = tmux.list_panes(window_name)
    session_by_pane = {session.pane_id: session for session in sessions if session.pane_id}
    live_pane_set = set(live_panes)
    bridge_owned_panes = {
        pane_id
        for pane_id in bridge_owned_pane_ids(tmux, window_name=window_name)
        if pane_id in live_pane_set
    }
    orphan_panes = [
        pane_id
        for pane_id in live_panes
        if pane_id in bridge_owned_panes and pane_id not in session_by_pane
    ]
    for pane_id in orphan_panes:
        try:
            tmux.kill_pane(pane_id)
        except Exception as exc:
            logger.warning("%s failed to kill orphan pane %s: %s", log_context, pane_id, exc)
    return orphan_panes


def safe_list_panes(tmux, window_name: str) -> list[str]:
    try:
        panes = tmux.list_panes(window_name)
    except Exception as exc:
        logger.debug("list_panes failed for %s: %s", window_name, exc)
        return []
    if isinstance(panes, str):
        return [line.strip() for line in panes.splitlines() if line.strip()]
    if isinstance(panes, (list, tuple, set)):
        return [str(pane) for pane in panes if pane]
    return []


def bridge_owned_pane_ids(tmux, *, window_name: str = GENERAL_WINDOW_NAME) -> list[str]:
    list_pane_infos = getattr(tmux, "list_pane_infos", None)
    if not callable(list_pane_infos):
        return []
    try:
        pane_infos = list_pane_infos(window_name)
    except Exception as exc:
        logger.warning("Failed to inspect tmux pane ownership for %s: %s", window_name, exc)
        return []
    owned: list[str] = []
    for pane in pane_infos:
        is_bridge_owned = getattr(pane, "is_bridge_owned", None)
        if callable(is_bridge_owned):
            if is_bridge_owned():
                owned.append(pane.pane_id)
            continue
        if getattr(pane, "bridge_owned", "") == "1":
            owned.append(pane.pane_id)
    return owned


def _runtime_tmux(runtime: BridgeRuntime):
    lifecycle_tmux = getattr(getattr(runtime, "lifecycle", None), "tmux", None)
    worker_tmux = getattr(getattr(runtime, "worker", None), "tmux", None)
    return getattr(runtime, "tmux", None) or lifecycle_tmux or worker_tmux


def has_tmux_pane_capacity(store, tmux, *, pool_name: str | None = None) -> bool:
    windows = known_windows_for_pool(pool_name) if pool_name else all_known_window_names()
    capacity = capacity_for_pool(pool_name) if pool_name else MAX_CONCURRENT_SESSIONS
    list_all = getattr(store, "list_all", None)
    sessions = (
        [session for session in list_all() if session.status in RECOVERABLE_TMUX_STATUSES]
        if callable(list_all)
        else []
    )
    live_count = 0
    for window_name in windows:
        live_panes = safe_list_panes(tmux, window_name)
        reap_orphan_panes(
            store,
            tmux,
            window_name=window_name,
            sessions=sessions,
            live_panes=live_panes,
            log_context="Pre-allocation orphan pane reaper",
        )
        live_count += len(safe_list_panes(tmux, window_name))
    return live_count < capacity


def active_session_count_for_pool(store, pool_name: str) -> int:
    list_all = getattr(store, "list_all", None)
    if callable(list_all):
        return sum(
            1
            for session in list_all()
            if session.status in ACTIVE_CAPACITY_STATUSES and pool_for_session(session) == pool_name
        )
    active_count = getattr(store, "active_count", None)
    if callable(active_count):
        return int(active_count())
    return 0


def has_session_capacity(store, pool_name: str) -> bool:
    return active_session_count_for_pool(store, pool_name) < capacity_for_pool(pool_name)


def post_tmux_capacity_failure(
    runtime: BridgeRuntime, envelope: dict, *, pool_name: str | None = None
) -> None:
    capacity = capacity_for_pool(pool_name) if pool_name else MAX_CONCURRENT_SESSIONS
    runtime.post_to_slack(
        envelope["reply_target"]["channel_id"],
        envelope["reply_target"]["thread_ts"],
        render_message("tmux_pane_limit", _copy_context(capacity=capacity)),
    )
    runtime.state.update_event(
        envelope["event_id"],
        status="failed",
        conversation_identity=envelope["conversation_identity"],
        failure_reason="tmux_pane_capacity",
    )


def route_after_session_create_race(
    runtime: BridgeRuntime, session: Session, envelope: dict
) -> None:
    if session and is_optional_extension_pool_mismatch(session, envelope):
        runtime.state.update_event(
            envelope["event_id"],
            status="failed",
            conversation_identity=session.conversation_identity,
            failure_reason="optional_extension_pool_mismatch",
        )
        return
    if session.status is Status.ERROR:
        runtime.state.update_event(
            envelope["event_id"],
            status="failed",
            conversation_identity=envelope["conversation_identity"],
            failure_reason=f"session_{session.status.value}",
        )
        return
    if is_duplicate_optional_extension_notification(session, envelope):
        mark_duplicate_optional_extension_notification(runtime, session, envelope)
        return
    session = update_session_message_cursor(runtime, session, envelope)
    maybe_post_fixed_ack(runtime, envelope, session)
    say = make_say(runtime, session.channel_id, session.thread_ts)
    runtime.dispatch.route(session.conversation_identity, envelope, say)
    runtime.state.update_event(
        envelope["event_id"],
        status="queued",
        conversation_identity=session.conversation_identity,
    )


def create_session(runtime: BridgeRuntime, envelope: dict):
    pool_name = pool_for_envelope(envelope)
    tmux = _runtime_tmux(runtime)
    if tmux is not None and not has_tmux_pane_capacity(runtime.store, tmux, pool_name=pool_name):
        post_tmux_capacity_failure(runtime, envelope, pool_name=pool_name)
        return
    if not has_session_capacity(runtime.store, pool_name):
        capacity = capacity_for_pool(pool_name)
        runtime.post_to_slack(
            envelope["reply_target"]["channel_id"],
            envelope["reply_target"]["thread_ts"],
            render_message("session_limit", _copy_context(limit=capacity)),
        )
        runtime.state.update_event(
            envelope["event_id"],
            status="failed",
            conversation_identity=envelope["conversation_identity"],
            failure_reason="max_sessions",
        )
        return
    actor = envelope["actor"]
    channel = envelope["channel"]
    case_id = envelope.get("case_id") or f"case_{uuid.uuid4().hex}"
    session = Session.create(
        thread_ts=envelope["reply_target"]["thread_ts"],
        user_id=actor["user_id"],
        call_name=actor["call_name"],
        window_name=primary_window_for_pool(pool_name),
        case_id=case_id,
        conversation_identity=envelope["conversation_identity"],
        channel_id=channel["id"],
        team_id=envelope["team_id"],
        channel_name=channel.get("name", ""),
        channel_type=channel.get("type", "channel"),
        is_external_shared=channel.get("is_ext_shared", False),
    )
    session.worker_session_id = str(uuid.uuid4())
    session.last_event_id = envelope["event_id"]
    session.last_event_ts = envelope["source_message"]["ts"]
    session.last_thread_message_ts = envelope["source_message"]["ts"]
    create_if_absent = getattr(runtime.store, "create_if_absent", None)
    if callable(create_if_absent):
        created = create_if_absent(session)
    else:
        runtime.store.save(session)
        created = True
    if not created:
        existing = runtime.store.load(session.conversation_identity)
        if existing is None:
            runtime.state.update_event(
                envelope["event_id"],
                status="failed",
                conversation_identity=session.conversation_identity,
                failure_reason="session_create_race_lost_without_session",
            )
            return
        route_after_session_create_race(runtime, existing, envelope)
        return
    maybe_post_fixed_ack(runtime, envelope, None)
    say = make_say(runtime, session.channel_id, session.thread_ts)
    threading.Thread(
        target=runtime.worker.start, args=(session, envelope, say), daemon=True
    ).start()
    runtime.state.update_event(
        envelope["event_id"],
        status="queued",
        conversation_identity=session.conversation_identity,
    )


def update_session_message_cursor(
    runtime: BridgeRuntime, session: Session, envelope: dict
) -> Session:
    updates = {
        "last_activity_at": now_utc(),
        "last_event_id": envelope["event_id"],
        "last_event_ts": envelope["source_message"]["ts"],
    }
    if envelope["normalized_event_type"] in {"new_message", "thread_message", "direct_message"}:
        updates["last_thread_message_ts"] = envelope["source_message"]["ts"]
    return runtime.store.update_fields(session.conversation_identity, updates) or session


def is_authorized_kill_actor(session: Session, user_id: str) -> bool:
    return bool(user_id) and (user_id == session.user_id or user_id in config.ADMIN_USER_IDS)


def is_completion_reaction_actor(runtime: BridgeRuntime, session: Session, user_id: str) -> bool:
    return bool(user_id) and (
        user_id == getattr(runtime, "bot_user_id", "") or is_authorized_kill_actor(session, user_id)
    )


def is_valid_kill_target(session: Session, item_ts: str) -> bool:
    if not item_ts:
        return False
    if item_ts == session.thread_ts:
        return True
    return item_ts == session.last_thread_message_ts


def handle_completion_reaction(
    runtime: BridgeRuntime, session: Session | None, envelope: dict
) -> bool:
    if envelope["normalized_event_type"] != "reaction_added":
        return False
    if (envelope.get("reaction") or {}).get("name") != "white_check_mark":
        return False
    if session is None:
        runtime.state.update_event(
            envelope["event_id"],
            status="done",
            conversation_identity=envelope["conversation_identity"],
            failure_reason="reaction_without_session",
        )
        return True
    user_id = (envelope.get("actor") or {}).get("user_id", "")
    if not is_completion_reaction_actor(runtime, session, user_id):
        runtime.state.update_event(
            envelope["event_id"],
            status="done",
            conversation_identity=session.conversation_identity,
            failure_reason="completion_reaction_unauthorized",
        )
        return True
    item_ts = (envelope.get("reaction") or {}).get("item_ts", "")
    if not is_valid_kill_target(session, item_ts):
        runtime.state.update_event(
            envelope["event_id"],
            status="done",
            conversation_identity=session.conversation_identity,
            failure_reason="completion_reaction_target_ignored",
        )
        return True
    return apply_session_control(runtime, session, envelope["event_id"], "end")


def apply_session_control(
    runtime: BridgeRuntime, session: Session | None, event_id: str, control_command: str
) -> bool:
    if session is None:
        runtime.state.update_event(
            event_id,
            status="done",
            failure_reason="control_command_without_session",
        )
        return True

    latest = runtime.store.load(session.conversation_identity) or session
    if control_command == "cancel":
        updated = runtime.lifecycle.cancel(latest, reason="user_cancel")
        if updated is None:
            runtime.state.update_event(
                event_id,
                status="done",
                conversation_identity=latest.conversation_identity,
                failure_reason="user_cancel_ignored",
            )
            return True
        runtime.post_to_slack(
            updated.channel_id,
            updated.thread_ts,
            SESSION_INTERRUPTED_TEXT,
        )
        runtime.state.update_event(
            event_id,
            status="done",
            conversation_identity=updated.conversation_identity,
            failure_reason="user_cancel",
        )
        return True

    updated = runtime.lifecycle.kill(latest)
    if updated is None:
        runtime.state.update_event(
            event_id,
            status="done",
            conversation_identity=latest.conversation_identity,
            failure_reason="user_end_ignored",
        )
        return True
    runtime.post_to_slack(
        updated.channel_id,
        updated.thread_ts,
        SESSION_KILLED_TEXT,
    )
    runtime.state.update_event(
        event_id,
        status="done",
        conversation_identity=updated.conversation_identity,
        failure_reason="user_end",
    )
    return True


def handle_session_control(runtime: BridgeRuntime, session: Session | None, envelope: dict) -> bool:
    if not envelope_builder.is_message_event(envelope):
        return False
    control_command = detect_session_control_command(
        (envelope.get("source_message") or {}).get("text", "")
    )
    if control_command is None:
        return False
    return apply_session_control(runtime, session, envelope["event_id"], control_command)


def route_envelope(runtime: BridgeRuntime, envelope: dict):
    if handle_envelope_extensions(EXTENSIONS, runtime, envelope):
        return
    session = runtime.store.load(envelope["conversation_identity"])
    if handle_completion_reaction(runtime, session, envelope):
        return
    if handle_session_control(runtime, session, envelope):
        return
    if session and is_optional_extension_pool_mismatch(session, envelope):
        runtime.state.update_event(
            envelope["event_id"],
            status="failed",
            conversation_identity=session.conversation_identity,
            failure_reason="optional_extension_pool_mismatch",
        )
        return
    if (
        session
        and session.status is Status.KILLED
        and envelope_builder.is_session_revival_event(envelope)
    ):
        session = update_session_message_cursor(runtime, session, envelope)
        maybe_post_fixed_ack(runtime, envelope, session)
        say = make_say(runtime, session.channel_id, session.thread_ts)
        runtime.dispatch.recover_fn(session, envelope, say)
        runtime.state.update_event(
            envelope["event_id"],
            status="queued",
            conversation_identity=session.conversation_identity,
        )
        return
    if (
        session
        and envelope_builder.is_message_event(envelope)
        and is_recoverable_ready_timeout_error(session)
    ):
        revived = revive_recoverable_ready_timeout_session(runtime, session)
        session = revived or session
    if session and session.status is Status.ERROR:
        if envelope_builder.is_message_event(envelope):
            runtime.post_to_slack(
                session.channel_id,
                session.thread_ts,
                config.SESSION_ERROR_TEXT,
            )
        runtime.state.update_event(
            envelope["event_id"],
            status="failed",
            conversation_identity=envelope["conversation_identity"],
            failure_reason=f"session_{session.status.value}",
        )
        return
    if session and is_duplicate_optional_extension_notification(session, envelope):
        mark_duplicate_optional_extension_notification(runtime, session, envelope)
        return
    if session:
        session = update_session_message_cursor(runtime, session, envelope)
        maybe_post_fixed_ack(runtime, envelope, session)
        say = make_say(runtime, session.channel_id, session.thread_ts)
        runtime.dispatch.route(session.conversation_identity, envelope, say)
        runtime.state.update_event(
            envelope["event_id"],
            status="queued",
            conversation_identity=session.conversation_identity,
        )
        return
    if envelope["normalized_event_type"].startswith("reaction"):
        runtime.state.update_event(
            envelope["event_id"],
            status="done",
            conversation_identity=envelope["conversation_identity"],
            failure_reason="reaction_without_session",
        )
        return
    create_session(runtime, envelope)


def build_block_action_event_id(body: dict, action: dict) -> str:
    container = body.get("container") or {}
    message = body.get("message") or {}
    channel = body.get("channel") or {}
    user = body.get("user") or {}
    parts = [
        "block_action",
        body.get("trigger_id", ""),
        channel.get("id", "") or container.get("channel_id", ""),
        message.get("ts", "") or container.get("message_ts", ""),
        user.get("id", ""),
        action.get("action_id", "") or action.get("name", ""),
        action.get("action_ts", ""),
    ]
    return ":".join(parts)


def claim_block_action(
    runtime: BridgeRuntime, body: dict, action: dict, conversation_identity: str | None
) -> str | None:
    event_id = body.get("event_id") or build_block_action_event_id(body, action)
    claim = runtime.state.claim_event(
        event_id,
        envelope_id=body.get("trigger_id", ""),
        source_event_type="block_actions",
        conversation_identity=conversation_identity,
    )
    if not claim.claimed:
        return None
    return event_id


def block_action_target(body: dict) -> tuple[str, str, str | None]:
    channel = body.get("channel") or {}
    container = body.get("container") or {}
    message = body.get("message") or {}
    channel_id = channel.get("id", "") or container.get("channel_id", "")
    root_thread_ts = (
        message.get("thread_ts") or message.get("ts") or container.get("message_ts", "")
    )
    if not channel_id or not root_thread_ts:
        return channel_id, root_thread_ts, None
    return (
        channel_id,
        root_thread_ts,
        envelope_builder.build_conversation_identity(channel_id, root_thread_ts),
    )


def handle_block_action(runtime: BridgeRuntime, body: dict, ack=None) -> bool:
    if ack is not None:
        ack()
    actions = body.get("actions") or []
    action = actions[0] if actions else {}
    channel_id, root_thread_ts, conversation_identity = block_action_target(body)
    event_id = claim_block_action(runtime, body, action, conversation_identity)
    if event_id is None:
        return True

    action_id = action.get("action_id", "") or action.get("name", "")
    if action_id in SESSION_CANCEL_ACTION_IDS:
        session = runtime.store.load(conversation_identity) if conversation_identity else None
        return apply_session_control(runtime, session, event_id, "cancel")
    if action_id in SESSION_END_ACTION_IDS:
        session = runtime.store.load(conversation_identity) if conversation_identity else None
        return apply_session_control(runtime, session, event_id, "end")

    failure_reason = "unknown_block_action_ignored"
    if not actions:
        failure_reason = "empty_block_action_ignored"
    elif not channel_id or not root_thread_ts:
        failure_reason = "block_action_target_missing_ignored"
    runtime.state.update_event(
        event_id,
        status="done",
        conversation_identity=conversation_identity,
        failure_reason=failure_reason,
    )
    return True


def handle_inbound(runtime: BridgeRuntime, body: dict, builder):
    event_id = body.get("event_id", "")
    if not event_id:
        return
    event = body.get("event", {})
    claim = runtime.state.claim_event(
        event_id,
        envelope_id=body.get("envelope_id", ""),
        source_event_type=event.get("type", ""),
    )
    if not claim.claimed:
        return
    try:
        envelope = builder(runtime, body, event)
    except Exception as e:
        logger.exception("event normalization failed for %s: %s", event_id, e)
        runtime.state.update_event(event_id, status="failed", failure_reason=str(e))
        return
    if envelope is None:
        row = runtime.state.get_event(event_id)
        if row and row.get("status") == "received":
            runtime.state.update_event(event_id, status="done", failure_reason="filtered")
        return
    route_envelope(runtime, envelope)


def handle_app_mention(runtime: BridgeRuntime, body: dict, event: dict):
    handle_inbound(
        runtime,
        body,
        lambda rt, b, e: envelope_builder.build_message_envelope(
            rt, b, e, source_event_type="app_mention"
        ),
    )


def handle_channel_id_changed(runtime: BridgeRuntime, body: dict, event: dict):
    event_id = body.get("event_id", "")
    if event_id:
        claim = runtime.state.claim_event(
            event_id,
            envelope_id=body.get("envelope_id", ""),
            source_event_type="channel_id_changed",
        )
        if not claim.claimed:
            return
    old_channel_id = event.get("old_channel_id", "")
    new_channel_id = event.get("new_channel_id", "")
    invalidated = 0
    for session in runtime.store.list_all():
        if session.channel_id in {old_channel_id, new_channel_id} and session.status in {
            Status.CREATING,
            Status.STARTING,
            Status.READY,
            Status.BUSY,
            Status.IDLE,
            Status.WAITING,
            Status.SUSPENDED,
        }:
            runtime.store.update_fields(
                session.conversation_identity,
                {
                    "status": Status.ERROR,
                    "last_activity_at": now_utc(),
                    "failure_reason": "channel_id_changed",
                },
            )
            invalidated += 1
    if invalidated:
        logger.info(
            "channel_id_changed invalidated %s active session(s) for %s -> %s",
            invalidated,
            old_channel_id,
            new_channel_id,
        )
    if event_id:
        runtime.state.update_event(
            event_id,
            status="done",
            failure_reason=f"channel_id_changed_invalidated_{invalidated}",
        )


def handle_turn_complete(
    runtime: BridgeRuntime,
    *,
    case_id: str,
    window_name: str,
    session_id: str | None,
    pane_id: str | None = None,
    turn_id: str,
    action: str,
):
    session = runtime.store.find_by_field("worker_session_id", session_id) if session_id else None
    if session is None:
        session = runtime.store.find_by_field("case_id", case_id)
    if not session:
        return "not_found"
    if session_id and session.worker_session_id and session.worker_session_id != session_id:
        return "stale_session"
    if pane_id and session.pane_id != pane_id:
        return "stale_pane"
    if (
        window_name
        and session.window_name
        and (canonical_window_name(session.window_name) != canonical_window_name(window_name))
    ):
        return "stale_window"
    if session.status in {Status.ERROR, Status.KILLED}:
        return "terminal"
    if turn_id and session.active_turn_id and session.active_turn_id != turn_id:
        return "stale_turn"
    if turn_id and not session.active_turn_id:
        return "stale_turn"
    if session_id:
        session = (
            runtime.store.update_fields(
                session.conversation_identity, {"worker_session_id": session_id}
            )
            or session
        )
    event_id = session.active_turn_event_id
    completed = runtime.dispatch.complete_turn(session, action=action)
    if completed is None:
        return "noop"
    if action != "continue":
        turn_attempt = None
        turn_attempt = runtime.state.get_turn_attempt(turn_id)
        if not turn_attempt or not turn_attempt.get("completed_at"):
            runtime.state.mark_turn_attempt_completed(
                turn_id,
                completed_at=now_utc(),
                completion_action="error" if action == "error" else "close",
            )
    if action != "continue":
        cleanup_turn_context(turn_id)
    if event_id:
        runtime.state.update_event(
            event_id,
            status="done",
            conversation_identity=session.conversation_identity,
        )
    return completed.status.value


def register_handlers(runtime: BridgeRuntime):
    bridge_app, _ = _ensure_clients()

    @bridge_app.event("message")
    def on_message(body, event):
        is_bot_event = (
            event.get("subtype") == "bot_message"
            or bool(event.get("bot_id"))
            or event.get("user") == getattr(runtime, "bot_user_id", "")
        )
        if is_bot_event:
            if is_optional_extension_notification_message(event):
                handle_inbound(
                    runtime,
                    body,
                    lambda rt, b, e: envelope_builder.build_message_envelope(
                        rt, b, e, source_event_type="optional_extension_notification"
                    ),
                )
                return
            claim_and_mark_event_done(
                runtime,
                body,
                event,
                failure_reason="bot_message_ignored",
                source_event_type="message",
            )
            return
        if not envelope_builder.is_supported_message_subtype(event, source_event_type="message"):
            claim_and_mark_event_done(
                runtime,
                body,
                event,
                failure_reason="unsupported_message_subtype_ignored",
                source_event_type="message",
            )
            return
        handle_inbound(
            runtime,
            body,
            lambda rt, b, e: envelope_builder.build_message_envelope(
                rt, b, e, source_event_type="message"
            ),
        )

    @bridge_app.event("message_metadata_posted")
    def on_message_metadata_posted(body, event):
        if not is_optional_extension_notification_metadata(event.get("metadata")):
            return
        handle_inbound(
            runtime,
            body,
            lambda rt, b, e: envelope_builder.build_message_metadata_posted_envelope(rt, b, e),
        )

    @bridge_app.event("message_metadata_updated")
    def on_message_metadata_updated(body, event):
        del body, event
        return

    @bridge_app.event("message_metadata_deleted")
    def on_message_metadata_deleted(body, event):
        del body, event
        return

    @bridge_app.event("app_mention")
    def on_app_mention(body, event):
        handle_app_mention(runtime, body, event)

    @bridge_app.event("file_shared")
    def on_file_shared(body, event):
        claim_and_mark_event_done(
            runtime,
            body,
            event,
            failure_reason="file_shared_ignored",
            source_event_type="file_shared",
        )

    @bridge_app.event("file_created")
    def on_file_created(body, event):
        claim_and_mark_event_done(
            runtime,
            body,
            event,
            failure_reason="file_created_ignored",
            source_event_type="file_created",
        )

    @bridge_app.event("file_change")
    def on_file_change(body, event):
        claim_and_mark_event_done(
            runtime,
            body,
            event,
            failure_reason="file_change_ignored",
            source_event_type="file_change",
        )

    @bridge_app.event("file_public")
    def on_file_public(body, event):
        claim_and_mark_event_done(
            runtime,
            body,
            event,
            failure_reason="file_public_ignored",
            source_event_type="file_public",
        )

    @bridge_app.event("reaction_added")
    def on_reaction_added(body, event):
        handle_inbound(
            runtime,
            body,
            lambda rt, b, e: envelope_builder.build_reaction_envelope(
                rt, b, e, source_event_type="reaction_added"
            ),
        )

    @bridge_app.event("reaction_removed")
    def on_reaction_removed(body, event):
        del body, event
        return

    action_registrar = getattr(bridge_app, "action", None)
    if callable(action_registrar):

        @action_registrar(re.compile(".*"))
        def on_block_action(ack, body):
            handle_block_action(runtime, body, ack=ack)

    @bridge_app.event("channel_id_changed")
    def on_channel_id_changed(body, event):
        handle_channel_id_changed(runtime, body, event)

    @bridge_app.event("member_joined_channel")
    def on_member_joined_channel(body, event):
        claim_and_mark_event_done(
            runtime,
            body,
            event,
            failure_reason="member_joined_channel_ignored",
            source_event_type="member_joined_channel",
        )


def run_cleanup_pass(runtime: BridgeRuntime) -> None:
    try:
        runtime.lifecycle.cleanup_stale()
    except Exception as e:
        logger.exception("Cleanup loop failed: %s", e)
    try:
        runtime.state.prune_event_ledger()
    except Exception as e:
        logger.warning("event ledger prune failed: %s", e)
    try:
        runtime.state.prune_bootstrap_ledger()
    except Exception as e:
        logger.warning("bootstrap ledger prune failed: %s", e)


def cleanup_loop(runtime: BridgeRuntime):
    while True:
        time.sleep(60)
        run_cleanup_pass(runtime)


def startup_cleanup(store, tmux, lifecycle: SessionLifecycle):
    reap_idle_sessions(store=store, tmux=tmux)

    recoverable_statuses = RECOVERABLE_TMUX_STATUSES - {Status.CREATING}
    sessions = [session for session in store.list_all() if session.status in recoverable_statuses]
    live_panes_by_window = {
        window_name: safe_list_panes(tmux, window_name) for window_name in all_known_window_names()
    }
    live_panes = [pane_id for panes in live_panes_by_window.values() for pane_id in panes]
    live_pane_set = set(live_panes)
    orphan_panes: list[str] = []
    for window_name, window_live_panes in live_panes_by_window.items():
        orphan_panes.extend(
            reap_orphan_panes(
                store,
                tmux,
                window_name=window_name,
                sessions=sessions,
                live_panes=window_live_panes,
                log_context="Startup cleanup",
            )
        )
    suspended_sessions: list[str] = []
    state = lifecycle.state

    def has_runtime_fields(session: Session) -> bool:
        return any(
            (
                session.pane_id,
                session.active_turn_id,
                session.active_turn_event_id,
                session.active_turn_owner_user_id,
                session.active_turn_started_at,
                session.input_wait,
            )
        )

    def persist_session_updates(session: Session, updates: dict) -> Session:
        if hasattr(store, "update_fields"):
            updated = store.update_fields(session.conversation_identity, updates)
            if isinstance(updated, Session):
                return updated
        for key, value in updates.items():
            setattr(session, key, value)
        store.save(session)
        return session

    def clear_suspended_runtime(session: Session) -> None:
        if session.pane_id and session.pane_id in live_pane_set:
            try:
                tmux.kill_pane(session.pane_id)
            except Exception as exc:
                logger.warning(
                    "Startup cleanup failed to kill suspended pane %s for %s: %s",
                    session.pane_id,
                    session.conversation_identity,
                    exc,
                )
        lifecycle.finalize_session_turns(session, "startup_cleanup_suspended")
        updated = persist_session_updates(
            session,
            {
                "input_wait": None,
                "last_activity_at": now_utc(),
                **clear_turn_fields(),
            },
        )
        if updated.pane_id:
            updated = lifecycle.clear_pane_after_teardown(updated)
        lifecycle.input_detector.clear_session(updated.thread_ts)

    for session in sessions:
        if session.status is Status.SUSPENDED:
            if has_runtime_fields(session):
                clear_suspended_runtime(session)
            continue
        missing_pane = not session.pane_id or session.pane_id not in live_pane_set
        shell_prompt_only = False
        failure_reason = "startup_cleanup_restart"
        active_turn_id = session.active_turn_id
        if session.status is Status.STARTING and not missing_pane and session.pane_id:
            try:
                output = tmux.capture_pane(session.pane_id, lines=20)
            except Exception as exc:
                logger.debug("Startup cleanup capture failed for %s: %s", session.pane_id, exc)
                output = ""
            lines = [line.rstrip() for line in output.splitlines() if line.strip()]
            if lines:
                last_line = lines[-1].strip()
                shell_prompt_only = bool(re.search(r"[$%❯]\s*$", last_line)) and not any(
                    marker in "\n".join(lines)
                    for marker in ("Codex", "? for shortcuts", "[Pasted text #")
                )
            if shell_prompt_only:
                failure_reason = "startup_cleanup_shell_prompt"
        updates = {
            "failure_reason": failure_reason,
            "input_wait": None,
            **clear_turn_fields(),
        }
        updated = lifecycle.transition(
            session,
            Status.SUSPENDED,
            reason=failure_reason,
            expected_status=session.status,
            updates=updates,
            teardown=not missing_pane,
        )
        if updated is not None:
            lifecycle.finalize_session_turns(session, failure_reason, turn_id=active_turn_id)
            if updated.pane_id:
                updated = lifecycle.clear_pane_after_teardown(updated)
            suspended_sessions.append(updated.conversation_identity)

    logger.info(
        "Startup cleanup: reaped %s orphan pane(s) panes=%s",
        len(orphan_panes),
        ", ".join(orphan_panes) if orphan_panes else "none",
    )
    logger.info(
        "Startup cleanup: suspended %s recoverable session(s) reason=startup_cleanup_restart sessions=%s",
        len(suspended_sessions),
        ", ".join(suspended_sessions) if suspended_sessions else "none",
    )
    if state is not None:
        state.increment_runtime_counter("orphan_panes_reaped", len(orphan_panes))
        state.increment_runtime_counter(
            "orphaned_sessions",
            sum(
                1
                for session in sessions
                if not session.pane_id or session.pane_id not in live_pane_set
            ),
        )


def drain_durable_event_queue(runtime: BridgeRuntime, *, limit: int = 100) -> dict[str, int]:
    def is_startup_suspended(session: Session) -> bool:
        return (session.failure_reason or "").startswith("startup_cleanup_")

    def is_optional_extension_pool_waiting(session: Session) -> bool:
        return (session.failure_reason or "").startswith(f"{OPTIONAL_EXTENSION_POOL_WAITING_REASON_PREFIX}:")

    def is_restart_terminated(session: Session) -> bool:
        return session.failure_reason == GRACEFUL_SHUTDOWN_REASON

    reset_count = runtime.state.reset_dispatching_event_queue_leases(
        reason="startup_reclaim_dispatching"
    )
    stats = {
        "reset": reset_count,
        "processed": 0,
        "failed": 0,
        "deferred": 0,
    }
    for row in runtime.state.list_resumable_event_queue(limit=limit):
        session = runtime.store.load(row["conversation_identity"])
        if session is None:
            if runtime.state.fail_event_queue_row(row["queue_id"], reason="missing_session"):
                stats["failed"] += 1
            continue
        if session.status is Status.SUSPENDED and is_startup_suspended(session):
            stats["deferred"] += 1
            continue
        if session.status is Status.SUSPENDED and is_optional_extension_pool_waiting(session):
            if not session.event_queue:
                if runtime.state.fail_event_queue_row(
                    row["queue_id"], reason="queue_payload_missing"
                ):
                    stats["failed"] += 1
                continue
            next_msg = session.event_queue[0]
            lease_owner = f"optional_extension-pool-retry:{session.conversation_identity}:{uuid.uuid4().hex}"
            claimed = runtime.store.claim_queue_head(
                session.conversation_identity,
                next_msg,
                lease_owner=lease_owner,
            )
            if claimed is None:
                stats["deferred"] += 1
                continue
            updated = runtime.store.transition(
                session.conversation_identity,
                Status.SUSPENDED,
                Status.STARTING,
                updates={
                    "window_name": primary_window_for_pool(pool_for_session(session)),
                    "pane_id": None,
                    "failure_reason": None,
                    "input_wait": None,
                    **clear_turn_fields(),
                },
            )
            if updated is None:
                runtime.store.mark_queue_retryable(
                    claimed["queue_id"],
                    last_error="optional_extension_pool_waiting_start_transition_failed",
                )
                stats["deferred"] += 1
                continue
            threading.Thread(
                target=runtime.worker.start,
                args=(
                    updated,
                    next_msg,
                    make_say(runtime, updated.channel_id, updated.thread_ts),
                ),
                kwargs={"queue_id": claimed["queue_id"]},
                daemon=True,
            ).start()
            stats["processed"] += 1
            continue
        if session.status is Status.SUSPENDED and is_restart_terminated(session):
            if not session.event_queue:
                if runtime.state.fail_event_queue_row(
                    row["queue_id"], reason="queue_payload_missing"
                ):
                    stats["failed"] += 1
                continue
            next_msg = session.event_queue[0]
            lease_owner = f"restart-retry:{session.conversation_identity}:{uuid.uuid4().hex}"
            claimed = runtime.store.claim_queue_head(
                session.conversation_identity,
                next_msg,
                lease_owner=lease_owner,
            )
            if claimed is None:
                stats["deferred"] += 1
                continue
            updated = runtime.store.transition(
                session.conversation_identity,
                Status.SUSPENDED,
                Status.STARTING,
                updates={
                    "window_name": primary_window_for_pool(pool_for_session(session)),
                    "pane_id": None,
                    "failure_reason": None,
                    "input_wait": None,
                    **clear_turn_fields(),
                },
            )
            if updated is None:
                runtime.store.mark_queue_retryable(
                    claimed["queue_id"],
                    last_error="restart_recovery_start_transition_failed",
                )
                stats["deferred"] += 1
                continue
            threading.Thread(
                target=runtime.worker.start,
                args=(
                    updated,
                    next_msg,
                    make_say(runtime, updated.channel_id, updated.thread_ts),
                ),
                kwargs={"queue_id": claimed["queue_id"], "recovery_mode": True},
                daemon=True,
            ).start()
            stats["processed"] += 1
            continue
        if session.status in {Status.ERROR, Status.KILLED, Status.SUSPENDED}:
            reason = f"session_{session.status.value}"
            if runtime.state.fail_event_queue_row(row["queue_id"], reason=reason):
                stats["failed"] += 1
            continue
        if session.status in {Status.READY, Status.IDLE}:
            if not session.event_queue:
                if runtime.state.fail_event_queue_row(
                    row["queue_id"], reason="queue_payload_missing"
                ):
                    stats["failed"] += 1
                continue
            runtime.dispatch._drain_queue(session)
            stats["processed"] += 1
            continue
        stats["deferred"] += 1
    logger.info(
        "Durable queue drain: reset=%s processed=%s failed=%s deferred=%s",
        stats["reset"],
        stats["processed"],
        stats["failed"],
        stats["deferred"],
    )
    return stats


def shutdown_all(runtime: BridgeRuntime):
    logger.info("Shutting down active sessions")
    for session in runtime.store.list_all():
        if session.is_active():
            updated = runtime.lifecycle.terminate_for_restart(
                session, reason=GRACEFUL_SHUTDOWN_REASON
            )
            if updated is None:
                logger.warning(
                    "Graceful shutdown skipped session identity=%s status=%s",
                    session.conversation_identity,
                    session.status.value,
                )
    logger.info("Shutdown complete")


def make_signal_handler(runtime: BridgeRuntime):
    def handle_signal(signum, frame):
        shutdown_all(runtime)
        raise SystemExit(0)

    return handle_signal


def make_recover_session_handler(
    store: SessionStore, lifecycle: SessionLifecycle, worker: WorkerProcess
):
    def recover_session(session: Session, payload: dict | str, say):
        assert session.conversation_identity, "conversation_identity missing during recovery"
        pool_name = pool_for_session(session)
        tmux = getattr(lifecycle, "tmux", None) or getattr(worker, "tmux", None)
        if tmux is not None and not has_tmux_pane_capacity(store, tmux, pool_name=pool_name):
            capacity = capacity_for_pool(pool_name)
            say(
                render_message("tmux_pane_limit", _copy_context(capacity=capacity)),
                thread_ts=session.thread_ts,
            )
            return
        if not has_session_capacity(store, pool_name):
            capacity = capacity_for_pool(pool_name)
            say(
                render_message("session_limit", _copy_context(limit=capacity)),
                thread_ts=session.thread_ts,
            )
            return
        new_session_id = str(uuid.uuid4())
        updated = lifecycle.transition(
            session,
            Status.STARTING,
            reason="recover_session",
            expected_status={Status.SUSPENDED, Status.KILLED},
            updates={
                "window_name": primary_window_for_pool(pool_name),
                "pane_id": None,
                "worker_session_id": new_session_id,
                "failure_reason": None,
                "input_wait": None,
                **clear_turn_fields(),
            },
            teardown=False,
        )
        if updated is None or updated.status is Status.ERROR:
            say(RESUME_FAILED_TEXT, thread_ts=session.thread_ts)
            return
        assert updated.conversation_identity == session.conversation_identity, (
            f"conversation_identity drift during recovery: "
            f"{session.conversation_identity} -> {updated.conversation_identity}"
        )
        say(RECOVERY_PROGRESS_TEXT, thread_ts=session.thread_ts)
        threading.Thread(
            target=worker.recover,
            args=(updated, payload, say),
            daemon=True,
        ).start()

    return recover_session


def _resolve_derived_values():
    """auth.test API で BOT_USER_ID と TEAM_ID を解決し config に反映する。"""
    bridge_app, _ = _ensure_clients()
    if config.TEST_MODE:
        config.SLACK_BOT_USER_ID = os.environ.get("SLACK_BOT_USER_ID", "U_TEST_BOT")
        config.SLACK_TEAM_ID = os.environ.get("SLACK_TEAM_ID", "T_TEST")
        logger.info(
            "Resolved test bot_user_id=%s, team_id=%s",
            config.SLACK_BOT_USER_ID,
            config.SLACK_TEAM_ID,
        )
        return
    resp = bridge_app.client.auth_test()
    config.SLACK_BOT_USER_ID = resp["user_id"]
    config.SLACK_TEAM_ID = resp["team_id"]
    logger.info(
        "Resolved bot_user_id=%s, team_id=%s", config.SLACK_BOT_USER_ID, config.SLACK_TEAM_ID
    )


def main():
    config.resolve_runtime_paths()
    bridge_app, bridge_user_client = _ensure_clients()
    logger.info("Slack Bridge starting")
    logger.info("tmux: %s", TMUX_SESSION_NAME)
    logger.info("Max sessions: %s", MAX_CONCURRENT_SESSIONS)

    _resolve_derived_values()

    state = BridgeState()
    store = SessionStore()
    tmux = TmuxGateway()

    post_to_slack = slack_api.make_post_to_slack(
        bridge_app.client,
        store,
        state,
        conversation_identity_for=envelope_builder.build_conversation_identity,
    )
    interactive = InteractiveHandler(tmux, store, post_to_slack, state=state)
    detector = InputDetector(tmux, store, interactive.on_input_wait_detected)
    worker = WorkerProcess(tmux, store, state=state)
    lifecycle = SessionLifecycle(tmux, store, worker, None, detector, post_to_slack, state=state)
    startup_cleanup(store, tmux, lifecycle)

    recover_session = make_recover_session_handler(store, lifecycle, worker)

    dispatch = MessageDispatch(
        tmux, store, interactive, detector, post_to_slack, recover_session, state=state
    )
    lifecycle.dispatch = dispatch
    add_reaction = slack_api.make_add_reaction(bridge_app.client)
    runtime = BridgeRuntime(
        store,
        state,
        worker,
        dispatch,
        lifecycle,
        post_to_slack,
        config.SLACK_BOT_USER_ID,
        bridge_app.client,
        bridge_user_client,
        add_reaction=add_reaction,
    )
    drain_durable_event_queue(runtime)

    signal_handler = make_signal_handler(runtime)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    register_handlers(runtime)
    hook_post_to_slack = make_outbound_pipeline(
        post_to_slack,
        EXTENSIONS,
        store=store,
        dispatch=dispatch,
        conversation_identity_for=envelope_builder.build_conversation_identity,
    )
    start_hook_server(
        HookBridge(
            store=store,
            state=state,
            post_to_slack=hook_post_to_slack,
            add_reaction=add_reaction,
            handle_turn_complete=lambda **kwargs: handle_turn_complete(runtime, **kwargs),
        )
    )
    detector.start()
    threading.Thread(target=cleanup_loop, args=(runtime,), daemon=True).start()

    logger.info("Listening for Slack events")
    SocketModeHandler(bridge_app, config.SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
