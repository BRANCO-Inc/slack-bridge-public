"""Slack event to envelope conversion."""

from __future__ import annotations

from slack_sdk.errors import SlackApiError

import config
import slack_api
from bridge_logging import get_logger
from config import (
    AI_BOOT_REACTION_NAME,
    AI_BOOT_THREAD_CONTEXT_LIMIT,
    CALL_NAME_MAP,
    SLACK_BOT_TOKEN,
)
from extensions.ai_boot import AI_BOOT_INSTRUCTION, is_ai_boot_reaction_event

logger = get_logger(__name__)
SUPPORTED_MESSAGE_SUBTYPES = {"file_share", "thread_broadcast"}


def build_conversation_identity(channel_id: str, root_thread_ts: str) -> str:
    return f"{config.SLACK_TEAM_ID}:{channel_id}:{root_thread_ts}"


def build_mention_dedupe_key(channel_id: str, message_ts: str) -> str:
    return f"{config.SLACK_TEAM_ID}:{channel_id}:{message_ts}"


def is_message_event(envelope: dict) -> bool:
    return envelope.get("normalized_event_type") in {
        "new_message",
        "thread_message",
        "direct_message",
    }


def is_session_revival_event(envelope: dict) -> bool:
    return is_message_event(envelope) or is_ai_boot_reaction_event(envelope)


def is_supported_message_subtype(event: dict) -> bool:
    subtype = event.get("subtype")
    if not subtype:
        return True
    return subtype in SUPPORTED_MESSAGE_SUBTYPES


def is_bridge_authored_message(message: dict, bot_user_id: str) -> bool:
    if not message:
        return False
    if bot_user_id and message.get("user") == bot_user_id:
        return True
    if message.get("bot_id"):
        return True
    return message.get("subtype") == "bot_message"


def choose_thread_client(runtime, channel_type: str):
    if channel_type in {"channel", "group"}:
        return runtime.user_client
    return runtime.app_client


def fetch_channel_info(runtime, body: dict, event: dict) -> dict:
    channel_id = event.get("channel", "")
    channel_type = event.get("channel_type", "")
    outer_ext_shared = body.get("is_ext_shared_channel")
    ext_shared_known = isinstance(outer_ext_shared, bool) or channel_type in {"im", "mpim"}
    info = {
        "id": channel_id,
        "type": channel_type,
        "name": "",
        "is_private": channel_type == "group",
        "is_im": channel_type == "im",
        "is_mpim": channel_type == "mpim",
        "is_ext_shared": bool(outer_ext_shared),
        "is_ext_shared_known": ext_shared_known,
    }
    if not channel_id:
        return info
    try:
        client = runtime.user_client if channel_type in {"channel", "group"} else runtime.app_client
        response = slack_api.slack_read_call(client, "conversations_info", channel=channel_id)
        channel = response.get("channel", {})
        info.update(
            {
                "name": channel.get("name", info["name"]),
                "is_private": channel.get("is_private", info["is_private"]),
                "is_im": channel.get("is_im", info["is_im"]),
                "is_mpim": channel.get("is_mpim", info["is_mpim"]),
                "is_ext_shared": channel.get("is_ext_shared", info["is_ext_shared"]),
                "is_ext_shared_known": True,
            }
        )
        if not info["type"]:
            if info["is_im"]:
                info["type"] = "im"
            elif info["is_mpim"]:
                info["type"] = "mpim"
            elif info["is_private"]:
                info["type"] = "group"
            else:
                info["type"] = "channel"
    except slack_api.SlackApiCallFailure as e:
        if e.classification in {"transient", "retry_exhausted", "exception"}:
            raise
        logger.warning("conversations_info failed for %s: %s", channel_id, e)
    except SlackApiError as e:
        logger.warning("conversations_info failed for %s: %s", channel_id, e)
    return info


def fetch_actor(runtime, user_id: str) -> dict:
    if not user_id:
        return {"user_id": "", "display_name": "", "call_name": "", "is_stranger": False}
    try:
        response = runtime.user_client.users_info(user=user_id)
        user = response.get("user", {})
        profile = user.get("profile", {})
        display_name = (
            profile.get("display_name")
            or profile.get("real_name")
            or CALL_NAME_MAP.get(user_id, user_id)
        )
        return {
            "user_id": user_id,
            "display_name": display_name,
            "call_name": CALL_NAME_MAP.get(user_id, display_name),
            "is_stranger": user.get("is_stranger", False),
        }
    except SlackApiError as e:
        logger.warning("users_info failed for %s: %s", user_id, e)
        display_name = CALL_NAME_MAP.get(user_id, user_id)
        return {
            "user_id": user_id,
            "display_name": display_name,
            "call_name": display_name,
            "is_stranger": False,
            "actor_from_memory": True,
        }


def fetch_message_snapshot(runtime, channel_info: dict, ts: str) -> dict:
    client = choose_thread_client(runtime, channel_info["type"])
    response = slack_api.slack_read_call(
        client,
        "conversations_history",
        channel=channel_info["id"],
        latest=ts,
        oldest=ts,
        inclusive=True,
        limit=1,
    )
    messages = response.get("messages", [])
    if not messages:
        raise RuntimeError("message_snapshot_not_found")
    return messages[0]


def fetch_reaction_message_snapshot(runtime, channel_info: dict, ts: str) -> dict:
    response = slack_api.slack_read_call(
        runtime.app_client,
        "reactions_get",
        channel=channel_info["id"],
        timestamp=ts,
        full=True,
    )
    message = response.get("message")
    if not isinstance(message, dict) or not message:
        raise RuntimeError("reaction_message_snapshot_not_found")
    return message


def fetch_thread_snapshot(
    runtime,
    channel_info: dict,
    *,
    root_ts: str,
    target_ts: str,
    limit: int,
    fetch_all: bool = False,
) -> tuple[dict, dict, list[dict], bool]:
    client = choose_thread_client(runtime, channel_info["type"])
    messages: list[dict] = []
    cursor = None
    fetched_all = False
    while True:
        kwargs = {
            "channel": channel_info["id"],
            "ts": root_ts,
            "limit": limit,
            "include_all_metadata": True,
        }
        if cursor:
            kwargs["cursor"] = cursor
        response = slack_api.slack_read_call(client, "conversations_replies", **kwargs)
        messages.extend(response.get("messages", []))
        has_more = bool(response.get("has_more"))
        target_found = any(message.get("ts") == target_ts for message in messages)
        if not has_more:
            fetched_all = True
            break
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
        if fetch_all or not target_found:
            continue
        break
    if not messages:
        raise RuntimeError("empty_thread_snapshot")
    root = next((m for m in messages if m.get("ts") == root_ts), messages[0])
    target = next((m for m in messages if m.get("ts") == target_ts), None)
    if target is None:
        raise RuntimeError("target_message_not_found")
    return target, root, messages, fetched_all


def build_thread_context(messages: list[dict], root_ts: str, *, truncated: bool) -> dict:
    return {
        "root_ts": root_ts,
        "message_count": len(messages),
        "truncated": truncated,
        "messages": [
            {
                "ts": message.get("ts"),
                "thread_ts": message.get("thread_ts"),
                "user": message.get("user"),
                "text": message.get("text", ""),
            }
            for message in messages
        ],
    }


def is_allowed_slack_connect_channel(channel_info: dict) -> bool:
    if channel_info.get("is_ext_shared_known") is False:
        return False
    if not channel_info.get("is_ext_shared"):
        return True
    if "*" in config.SLACK_CONNECT_ALLOWED_CHANNEL_NAME_PREFIXES:
        return True
    channel_name = str(channel_info.get("name") or "")
    return any(
        channel_name.startswith(prefix)
        for prefix in config.SLACK_CONNECT_ALLOWED_CHANNEL_NAME_PREFIXES
    )


def claim_mention_delivery(
    runtime,
    body: dict,
    event: dict,
    *,
    channel_info: dict,
    source_event_type: str,
    text: str,
) -> bool:
    if channel_info["type"] == "im":
        return True
    if source_event_type == "app_mention":
        pass
    elif not slack_api.has_bot_mention(runtime.bot_user_id, text):
        return True
    message_ts = event.get("ts", "")
    channel_id = channel_info.get("id", "")
    if not message_ts or not channel_id:
        return True
    mention_key = build_mention_dedupe_key(channel_id, message_ts)
    if runtime.state.claim_bootstrap(
        mention_key,
        event_id=body["event_id"],
        source_event_type=source_event_type,
    ):
        return True
    runtime.state.update_event(body["event_id"], status="done", failure_reason="mention_duplicate")
    return False


def build_message_envelope(
    runtime, body: dict, event: dict, *, source_event_type: str
) -> dict | None:
    channel_info = fetch_channel_info(runtime, body, event)
    if not is_allowed_slack_connect_channel(channel_info):
        runtime.state.update_event(
            body["event_id"], status="done", failure_reason="external_shared_ignored"
        )
        return None
    if not is_supported_message_subtype(event):
        runtime.state.update_event(
            body["event_id"], status="done", failure_reason="unsupported_message_subtype_ignored"
        )
        return None
    text = event.get("text", "").strip()
    files = event.get("files", [])
    if not text and not files:
        runtime.state.update_event(
            body["event_id"], status="done", failure_reason="empty_message_ignored"
        )
        return None

    is_thread = bool(event.get("thread_ts"))
    existing_identity = build_conversation_identity(
        channel_info["id"], event.get("thread_ts") or event.get("ts", "")
    )
    has_existing_session = bool(runtime.store.load(existing_identity))
    mentioned = slack_api.has_bot_mention(runtime.bot_user_id, text)
    if source_event_type == "message":
        if channel_info["type"] == "im":
            pass
        elif channel_info["type"] == "mpim" and not mentioned:
            runtime.state.update_event(
                body["event_id"], status="done", failure_reason="mpim_requires_mention"
            )
            return None
        elif not is_thread and not mentioned:
            runtime.state.update_event(
                body["event_id"], status="done", failure_reason="not_addressed_to_bridge"
            )
            return None
    if not claim_mention_delivery(
        runtime,
        body,
        event,
        channel_info=channel_info,
        source_event_type=source_event_type,
        text=text,
    ):
        return None

    files_paths, file_downloads = slack_api.download_slack_files_with_diagnostics(
        files, SLACK_BOT_TOKEN
    )
    metadata = event.get("metadata") or {}
    bootstrap_thread = is_thread and not has_existing_session
    root_ts = event.get("thread_ts") or event["ts"]
    target, root, messages, truncated = fetch_thread_snapshot(
        runtime,
        channel_info,
        root_ts=root_ts,
        target_ts=event["ts"],
        limit=200 if bootstrap_thread else 20,
        fetch_all=bootstrap_thread,
    )
    if (
        source_event_type == "message"
        and is_thread
        and not has_existing_session
        and not mentioned
        and not is_bridge_authored_message(root, runtime.bot_user_id)
    ):
        runtime.state.update_event(
            body["event_id"], status="done", failure_reason="thread_without_session"
        )
        return None
    actor = fetch_actor(runtime, event.get("user", ""))
    target_text = str(target.get("text", text) or "")
    source_text = slack_api.strip_mention(target_text) if mentioned else target_text
    root_thread_ts = str(root.get("thread_ts") or root.get("ts") or "")
    conversation_identity = build_conversation_identity(channel_info["id"], root_thread_ts)
    runtime.state.update_event(
        body["event_id"],
        status="context_ready",
        conversation_identity=conversation_identity,
    )
    envelope = {
        "event_id": body["event_id"],
        "envelope_id": body.get("envelope_id", ""),
        "source_event_type": f"message.{channel_info['type']}",
        "delivery_event_type": source_event_type,
        "normalized_event_type": "direct_message"
        if channel_info["type"] == "im"
        else ("thread_message" if is_thread else "new_message"),
        "conversation_identity": conversation_identity,
        "team_id": config.SLACK_TEAM_ID,
        "channel": channel_info,
        "actor": actor,
        "source_message": {
            "ts": target.get("ts"),
            "thread_ts": target.get("thread_ts") or root_thread_ts,
            "text": source_text,
            "permalink": None,
            "files": files_paths,
            "file_downloads": file_downloads,
            "file_download_failures": [item for item in file_downloads if not item.get("ok")],
            "metadata": metadata,
        },
        "reaction": None,
        "thread_context": build_thread_context(messages, root_thread_ts, truncated=truncated),
        "reply_target": {
            "channel_id": channel_info["id"],
            "thread_ts": root_thread_ts,
        },
    }
    return envelope


def claim_ai_boot_reaction(runtime, body: dict, *, channel_id: str, item_ts: str) -> bool:
    bootstrap_key = f"ai_boot:{config.SLACK_TEAM_ID}:{channel_id}:{item_ts}"
    if not runtime.state.claim_bootstrap(
        bootstrap_key,
        event_id=body["event_id"],
        source_event_type="reaction.message",
    ):
        runtime.state.update_event(
            body["event_id"], status="done", failure_reason="duplicate_ai_boot_reaction"
        )
        return False
    return True


def build_ai_boot_reaction_envelope(runtime, body: dict, event: dict) -> dict | None:
    item = event.get("item", {})
    channel_id = item.get("channel", "")
    item_ts = item.get("ts", "")
    if event.get("user") == getattr(runtime, "bot_user_id", ""):
        runtime.state.update_event(
            body["event_id"], status="done", failure_reason="self_reaction_ignored"
        )
        return None
    channel_event = {**event, "channel": channel_id}
    channel_info = fetch_channel_info(runtime, body, channel_event)
    if not is_allowed_slack_connect_channel(channel_info):
        runtime.state.update_event(
            body["event_id"], status="done", failure_reason="external_shared_ignored"
        )
        return None
    actor = fetch_actor(runtime, event.get("user", ""))
    target_message = fetch_reaction_message_snapshot(runtime, channel_info, item_ts)
    root_ts = target_message.get("thread_ts") or target_message.get("ts") or item_ts
    _target, _root, messages, fetched_all = fetch_thread_snapshot(
        runtime,
        channel_info,
        root_ts=root_ts,
        target_ts=item_ts,
        limit=AI_BOOT_THREAD_CONTEXT_LIMIT,
        fetch_all=True,
    )
    conversation_identity = build_conversation_identity(channel_info["id"], root_ts)
    if not claim_ai_boot_reaction(runtime, body, channel_id=channel_id, item_ts=item_ts):
        return None
    runtime.state.update_event(
        body["event_id"],
        status="context_ready",
        conversation_identity=conversation_identity,
    )
    return {
        "event_id": body["event_id"],
        "envelope_id": body.get("envelope_id", ""),
        "source_event_type": "reaction.message",
        "delivery_event_type": "reaction_added",
        "normalized_event_type": "ai_boot_reaction",
        "conversation_identity": conversation_identity,
        "team_id": config.SLACK_TEAM_ID,
        "channel": channel_info,
        "actor": actor,
        "source_message": {
            "ts": target_message.get("ts") or item_ts,
            "thread_ts": target_message.get("thread_ts") or root_ts,
            "text": target_message.get("text", ""),
            "permalink": None,
            "files": [],
            "metadata": {
                "ai_boot_instruction": AI_BOOT_INSTRUCTION,
            },
        },
        "reaction": {"name": AI_BOOT_REACTION_NAME, "item_ts": item_ts},
        "thread_context": build_thread_context(messages, root_ts, truncated=not fetched_all),
        "reply_target": {
            "channel_id": channel_info["id"],
            "thread_ts": root_ts,
        },
        "ai_boot_instruction": AI_BOOT_INSTRUCTION,
    }


def build_reaction_envelope(
    runtime, body: dict, event: dict, *, source_event_type: str
) -> dict | None:
    item = event.get("item", {})
    if item.get("type") != "message":
        runtime.state.update_event(
            body["event_id"], status="done", failure_reason="unsupported_reaction_item_type_ignored"
        )
        return None
    if source_event_type != "reaction_added":
        runtime.state.update_event(
            body["event_id"], status="done", failure_reason="unsupported_reaction"
        )
        return None
    if event.get("reaction") == AI_BOOT_REACTION_NAME:
        return build_ai_boot_reaction_envelope(runtime, body, event)
    if event.get("reaction") == "white_check_mark":
        channel_id = item.get("channel", "")
        item_ts = item.get("ts", "")
        conversation_identity = resolve_completion_reaction_identity(runtime, channel_id, item_ts)
        root_thread_ts = (
            conversation_identity.split(":", 2)[2]
            if conversation_identity.count(":") >= 2
            else item_ts
        )
        runtime.state.update_event(
            body["event_id"],
            status="context_ready",
            conversation_identity=conversation_identity,
        )
        return {
            "event_id": body["event_id"],
            "envelope_id": body.get("envelope_id", ""),
            "source_event_type": "reaction.message",
            "delivery_event_type": source_event_type,
            "normalized_event_type": "reaction_added",
            "conversation_identity": conversation_identity,
            "team_id": config.SLACK_TEAM_ID,
            "channel": {"id": channel_id},
            "actor": {"user_id": event.get("user", "")},
            "source_message": {"ts": item_ts, "thread_ts": root_thread_ts, "text": ""},
            "reaction": {"name": "white_check_mark", "item_ts": item_ts},
            "thread_context": None,
            "reply_target": {
                "channel_id": channel_id,
                "thread_ts": root_thread_ts,
            },
        }
    if event.get("user") == getattr(runtime, "bot_user_id", ""):
        runtime.state.update_event(
            body["event_id"], status="done", failure_reason="self_reaction_ignored"
        )
        return None
    runtime.state.update_event(
        body["event_id"], status="done", failure_reason="unsupported_reaction"
    )
    return None


def resolve_completion_reaction_identity(runtime, channel_id: str, item_ts: str) -> str:
    if not channel_id or not item_ts:
        return build_conversation_identity(channel_id, item_ts)
    list_all = getattr(getattr(runtime, "store", None), "list_all", None)
    if callable(list_all):
        try:
            for session in list_all():
                if getattr(session, "channel_id", "") != channel_id:
                    continue
                if item_ts in {session.thread_ts, getattr(session, "last_thread_message_ts", "")}:
                    return session.conversation_identity
        except Exception as exc:
            logger.debug("completion reaction session lookup failed: %s", exc)
    return build_conversation_identity(channel_id, item_ts)
