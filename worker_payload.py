"""worker へ送るプロンプト本文の組み立て（worker_process / message_dispatch 共用）。

旧 worker_process._format_message_for_agent / message_dispatch._payload_to_text の
二重実装の統合先。拡張のプロンプト注入（optional_extension 指示 / optional extension bootstrap）は
extensions の prepend_prompt フック経由で適用する。
"""

from __future__ import annotations

import json
from typing import Any

import extensions
from extensions.base import TurnContext, apply_prompt_extensions
from session import Session

RECOVERY_MODE_INSTRUCTION = (
    "[recovery_mode]\n"
    "Fresh worker. Use thread_context.messages as prior Slack context and source_message as latest instruction. "
    "Continue without mentioning recovery unless necessary.\n"
    "[/recovery_mode]"
)


def build_worker_payload(
    session: Session,
    message: dict | str,
    *,
    turn_id: str,
    reply_command_path: str,
    fresh_worker: bool = False,
    recovery_mode: bool = False,
    conversation_session: Any = None,
) -> str:
    """worker へ送る最終プロンプトを組み立てる。

    - fresh_worker=True: 新規 pane への初回/再開 turn（旧 worker_process 形式。
      詳細ヘッダ + 末尾指示文 + str 依頼の差出人行 + recovery_mode 指示）
    - fresh_worker=False: 稼働中 worker への追撃 turn（旧 message_dispatch 形式。
      最小ヘッダのみ）
    - conversation_session: prepend_prompt フックの TurnContext.session。
      既存会話への追撃 turn でのみ渡す（optional_extension 会話への bootstrap 強制注入判定）。
      初回/再開 turn は payload 判定のみで注入する（現行挙動の維持）。
    """
    if isinstance(message, str):
        return _format_text_message(
            session,
            message,
            turn_id=turn_id,
            reply_command_path=reply_command_path,
            fresh_worker=fresh_worker,
            recovery_mode=recovery_mode,
        )
    payload = _format_event_message(
        session,
        message,
        turn_id=turn_id,
        reply_command_path=reply_command_path,
        fresh_worker=fresh_worker,
        recovery_mode=recovery_mode,
    )
    turn_ctx = TurnContext(session=conversation_session, reply_command_path=reply_command_path)
    return apply_prompt_extensions(extensions.EXTENSIONS, payload, message, turn_ctx)


def _recovery_instruction(recovery_mode: bool) -> str:
    return f"\n{RECOVERY_MODE_INSTRUCTION}" if recovery_mode else ""


def _format_text_message(
    session: Session,
    message: str,
    *,
    turn_id: str,
    reply_command_path: str,
    fresh_worker: bool,
    recovery_mode: bool,
) -> str:
    if fresh_worker:
        return (
            f"[Slack依頼: {session.call_name}より]\n"
            f"[turn_id: {turn_id}]\n"
            f"[reply_command: {reply_command_path}]"
            f"{_recovery_instruction(recovery_mode)}\n"
            f"{message}"
        )
    header = []
    if turn_id:
        header.append(f"[turn_id: {turn_id}]")
    if reply_command_path:
        header.append(f"[reply_command: {reply_command_path}]")
    if not header:
        return message
    return "\n".join(header + [message])


def _format_event_message(
    session: Session,
    message: dict,
    *,
    turn_id: str,
    reply_command_path: str,
    fresh_worker: bool,
    recovery_mode: bool,
) -> str:
    event_json = json.dumps(message, ensure_ascii=False, indent=2)
    if fresh_worker:
        header = [
            f"[turn_id: {turn_id}]",
            f"[reply_command: {reply_command_path}]",
            "[Slack Bridge Event]",
            f"- reply_channel: {session.channel_id}",
            f"- reply_thread_ts: {session.thread_ts}",
            f"- conversation_identity: {session.conversation_identity}",
            f"- turn_id: {turn_id}",
            f"- event_type: {message.get('normalized_event_type', message.get('source_event_type', 'unknown'))}",
        ]
        return (
            "\n".join(header)
            + _recovery_instruction(recovery_mode)
            + "\n\n[event_envelope_json]\n"
            + event_json
            + "\n\n上のSlackイベント文脈を読み、必要ならSlackスレッドへ返信してください。"
        )
    header = []
    if turn_id:
        header.append(f"[turn_id: {turn_id}]")
    if reply_command_path:
        header.append(f"[reply_command: {reply_command_path}]")
    header.append("[Slack Bridge Event]")
    if turn_id:
        header.append(f"- turn_id: {turn_id}")
    return "\n".join(header) + "\n\n[event_envelope_json]\n" + event_json
