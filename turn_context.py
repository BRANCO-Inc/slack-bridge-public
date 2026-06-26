from __future__ import annotations

import json
import os
import shlex
import shutil
from dataclasses import dataclass

from config import (
    CASE_REPLY_CMD_PATH,
    HOOK_SERVER_PORT,
    TMP_DIR,
    TURN_CONTEXT_FILENAME,
    WORKSPACES_BASE,
)

TURN_ARTIFACTS_BASE = os.path.join(TMP_DIR, "turns")
TURN_CONTEXT_SNAPSHOT_FILENAME = "turn-context.json"
TURN_REPLY_WRAPPER_FILENAME = "case_reply.sh"


@dataclass(frozen=True)
class TurnArtifacts:
    latest_path: str | None
    snapshot_path: str
    reply_command_path: str


def turn_artifact_dir(turn_id: str) -> str:
    return os.path.join(TURN_ARTIFACTS_BASE, turn_id)


def _latest_context_filename(pane_id: str | None = None) -> str:
    if not pane_id:
        return TURN_CONTEXT_FILENAME
    safe_pane_id = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in pane_id)
    return f".slack-bridge-turn-{safe_pane_id}.json"


def turn_context_path(
    window_name: str, turn_id: str | None = None, pane_id: str | None = None
) -> str:
    if turn_id:
        return os.path.join(turn_artifact_dir(turn_id), TURN_CONTEXT_SNAPSHOT_FILENAME)
    return os.path.join(WORKSPACES_BASE, window_name, _latest_context_filename(pane_id))


def turn_reply_command_path(window_name: str, turn_id: str) -> str:
    del window_name
    return os.path.join(turn_artifact_dir(turn_id), TURN_REPLY_WRAPPER_FILENAME)


def cleanup_turn_context(turn_id: str) -> None:
    shutil.rmtree(turn_artifact_dir(turn_id), ignore_errors=True)


def _write_json_file(path: str, payload: dict[str, str]) -> None:
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp_path, path)


def write_turn_context(session, *, turn_id: str) -> TurnArtifacts:
    payload = {
        "case_id": session.case_id,
        "pane_id": session.pane_id or "",
        "session_id": session.worker_session_id or "",
        "window_name": session.window_name,
        "turn_id": turn_id,
    }
    snapshot_path = turn_context_path(session.window_name, turn_id)
    latest_path = turn_context_path(session.window_name, pane_id=session.pane_id)
    reply_command_path = turn_reply_command_path(session.window_name, turn_id)
    os.makedirs(TURN_ARTIFACTS_BASE, exist_ok=True)
    os.makedirs(turn_artifact_dir(turn_id), exist_ok=True)
    os.makedirs(os.path.dirname(latest_path), exist_ok=True)
    _write_json_file(snapshot_path, payload)
    _write_json_file(latest_path, payload)
    _write_reply_wrapper(reply_command_path, snapshot_path, payload)
    return TurnArtifacts(
        latest_path=latest_path,
        snapshot_path=snapshot_path,
        reply_command_path=reply_command_path,
    )


def _write_reply_wrapper(
    reply_command_path: str, snapshot_path: str, payload: dict[str, str]
) -> None:
    script = "\n".join(
        [
            "#!/bin/sh",
            "set -eu",
            f"export CC_TURN_CONTEXT_FILE={shlex.quote(snapshot_path)}",
            f"export CC_TURN_ID={shlex.quote(payload['turn_id'])}",
            f"export CC_CASE_ID={shlex.quote(payload['case_id'])}",
            f"export CC_WINDOW_NAME={shlex.quote(payload['window_name'])}",
            f"export SLACK_BRIDGE_WORKER_SESSION_ID={shlex.quote(payload['session_id'])}",
            f"export CC_PANE_ID={shlex.quote(payload['pane_id'])}",
            f'export BRIDGE_BASE_URL="${{BRIDGE_BASE_URL:-http://127.0.0.1:{HOOK_SERVER_PORT}}}"',
            f'exec {shlex.quote(CASE_REPLY_CMD_PATH)} "$@"',
            "",
        ]
    )
    with open(reply_command_path, "w", encoding="utf-8") as handle:
        handle.write(script)
    os.chmod(reply_command_path, 0o755)
