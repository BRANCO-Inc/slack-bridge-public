from __future__ import annotations

import json
import re
import shlex
import threading
import time
import uuid
from pathlib import Path

import config
import extensions
from bridge_logging import get_logger
from config import (
    BRIDGE_DIR,
    CASE_REPLY_CMD_PATH,
    CODEX_LAUNCH_ERROR_TEXT,
    CODEX_READY_TIMEOUT_TEXT,
    LIVE_WINDOW_WORKDIR,
    WORKER_READY_TIMEOUT,
)
from extensions.base import PoolSpec, find_pool_spec
from pane_pool import pool_for_session, windows_for_pool
from session import Session, Status, activate_turn_fields, clear_turn_fields, now_utc
from session_store import SessionStore
from tmux_gateway import PaneAllocationError, TmuxGateway
from turn_context import TurnArtifacts, cleanup_turn_context, turn_context_path, write_turn_context
from worker_payload import build_worker_payload

logger = get_logger(__name__)

TERMINAL_STATUSES = {Status.KILLED, Status.ERROR}
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
PANE_CLAIM_STATUSES = {
    Status.CREATING,
    Status.STARTING,
    Status.READY,
    Status.BUSY,
    Status.IDLE,
    Status.WAITING,
    Status.SUSPENDED,
}
_PANE_LAUNCH_LOCK = threading.RLock()
READY_TIMEOUT_SNAPSHOT_DIR = Path(config.TMP_DIR) / "ready-timeouts"
CODEX_READY = "CODEX_READY"
CODEX_READY_TIMEOUT = "CODEX_READY_TIMEOUT"
CODEX_UPDATE_RESTART_REQUIRED = "CODEX_UPDATE_RESTART_REQUIRED"
AGENT_INSTRUCTION_PROMPT = (
    "Before responding, read the Slack Bridge instruction files available in the current "
    "working directory: AGENTS.md when present, then IDENTITY.md and BRIDGE_PROTOCOL.md. "
    "Always use the per-turn reply_command when posting back to Slack."
)


class WorkerProcess:
    def __init__(self, tmux: TmuxGateway, store: SessionStore, *, state=None):
        self.tmux = tmux
        self.store = store
        self.state = state

    def _load_current(self, session: Session) -> Session:
        try:
            loaded = self.store.load(session.conversation_identity)
        except Exception:
            loaded = None
        return loaded if isinstance(loaded, Session) else session

    def _is_preclaimed_durable_queue_start(
        self,
        requested: Session,
        current: Session,
        queue_id: int | None,
    ) -> bool:
        return (
            queue_id is not None
            and requested.conversation_identity == current.conversation_identity
            and requested.status is Status.STARTING
            and current.status is Status.STARTING
            and requested.pane_id is None
            and current.pane_id is None
            and requested.active_turn_id is None
            and current.active_turn_id is None
            and requested.active_turn_queue_id is None
            and current.active_turn_queue_id is None
        )

    def start(
        self,
        session: Session,
        message: dict | str,
        say,
        *,
        queue_id: int | None = None,
        recovery_mode: bool = False,
    ):
        claimed = self.store.transition(
            session.conversation_identity,
            Status.CREATING,
            Status.STARTING,
        )
        if claimed is None:
            current = self._load_current(session)
            if self._is_preclaimed_durable_queue_start(session, current, queue_id):
                if recovery_mode:
                    self._launch(
                        current,
                        message,
                        say,
                        queue_id=queue_id,
                        recovery_mode=True,
                    )
                else:
                    self._launch(current, message, say, queue_id=queue_id)
                return
            logger.info(
                "skipping start; session already claimed: %s status=%s",
                session.conversation_identity,
                current.status.value,
            )
            return
        if recovery_mode:
            self._launch(claimed, message, say, queue_id=queue_id, recovery_mode=True)
        else:
            self._launch(claimed, message, say, queue_id=queue_id)

    def recover(
        self,
        session: Session,
        message: dict | str,
        say,
    ):
        assert session.conversation_identity, "conversation_identity missing during recovery"
        self._launch(session, message, say, recovery_mode=True)

    def _launch(
        self,
        session: Session,
        message: dict | str,
        say,
        *,
        queue_id: int | None = None,
        recovery_mode: bool = False,
    ):
        pane_id: str | None = None
        turn_id: str | None = None
        try:
            current = self._load_current(session)
            if current.status in TERMINAL_STATUSES:
                logger.info("skipping launch; session already terminal: %s", current.status.value)
                return
            turn_id = uuid.uuid4().hex
            ready_timeout = self._ready_timeout_for_session(session)
            update_restart_attempted = False
            while True:
                with _PANE_LAUNCH_LOCK:
                    pane_id = self._prepare_window(session)
                    self._start_command(session, pane_id, turn_id=turn_id)
                ready_state = self._wait_for_ready(
                    session.window_name,
                    pane_id,
                    timeout=ready_timeout,
                )
                if ready_state == CODEX_READY:
                    break
                if ready_state == CODEX_UPDATE_RESTART_REQUIRED and not update_restart_attempted:
                    update_restart_attempted = True
                    logger.warning(
                        "Codex update completed during launch; restarting once for %s",
                        session.thread_ts,
                    )
                    self._cleanup_update_restart_pane(pane_id)
                    continue
                failure_reason = self._ready_failure_reason(
                    ready_state, after_update=update_restart_attempted
                )
                if ready_state == CODEX_READY_TIMEOUT:
                    self._write_ready_timeout_snapshot(
                        session,
                        pane_id,
                        turn_id=turn_id,
                        timeout=ready_timeout,
                    )
                self._fail_session(
                    session,
                    pane_id,
                    reason=failure_reason,
                    turn_id=turn_id,
                    queue_id=queue_id,
                    status=Status.SUSPENDED if ready_state == CODEX_READY_TIMEOUT else Status.ERROR,
                )
                if ready_state == CODEX_READY_TIMEOUT:
                    say(self._timeout_message(session), thread_ts=session.thread_ts)
                else:
                    say(
                        self._error_message(session, RuntimeError(failure_reason)),
                        thread_ts=session.thread_ts,
                    )
                return
            self._dispatch_message(
                session,
                pane_id,
                message,
                turn_id=turn_id,
                expected_status=Status.STARTING,
                queue_id=queue_id,
                recovery_mode=recovery_mode,
            )
        except PaneAllocationError as e:
            suspend_spec = self._allocation_failure_suspend_spec(session, message)
            if suspend_spec is not None:
                logger.warning(
                    "%s pool allocation deferred for %s: %s",
                    suspend_spec.pool_name,
                    session.thread_ts,
                    e,
                )
                self._suspend_allocation_failure(
                    session,
                    message,
                    spec=suspend_spec,
                    reason=str(e),
                    turn_id=turn_id,
                    queue_id=queue_id,
                )
                return
            logger.exception("_launch failed for %s: %s", session.thread_ts, e)
            failure_reason = f"launch_failed: {e}"
            self._fail_session(
                session, pane_id, reason=failure_reason, turn_id=turn_id, queue_id=queue_id
            )
            say(self._error_message(session, e), thread_ts=session.thread_ts)
        except Exception as e:
            logger.exception("_launch failed for %s: %s", session.thread_ts, e)
            failure_reason = f"launch_failed: {e}"
            self._fail_session(
                session, pane_id, reason=failure_reason, turn_id=turn_id, queue_id=queue_id
            )
            say(self._error_message(session, e), thread_ts=session.thread_ts)

    def _ready_failure_reason(self, ready_state: str, *, after_update: bool) -> str:
        if ready_state == CODEX_READY_TIMEOUT:
            if after_update:
                return "launch_failed: ready_timeout_after_codex_update"
            return "launch_failed: ready_timeout"
        if ready_state == CODEX_UPDATE_RESTART_REQUIRED:
            return "launch_failed: repeated_codex_update_restart_required"
        return f"launch_failed: unexpected_ready_state:{ready_state}"

    def _cleanup_update_restart_pane(self, pane_id: str) -> None:
        try:
            self.tmux.kill_pane(pane_id)
        except Exception as exc:
            logger.warning(
                "Codex update pane cleanup failed before restart for %s: %s", pane_id, exc
            )

    def _allocation_failure_suspend_spec(
        self, session: Session, message: dict | str
    ) -> PoolSpec | None:
        """pane 割当失敗を SUSPENDED + 再キューへ逃がすべき場合に PoolSpec を返す。"""
        current = self._load_current(session)
        spec = self._pool_spec_for_session(current)
        if spec is None or spec.suspend_allocation_failure is None:
            return None
        return spec if spec.suspend_allocation_failure(message) else None

    def _suspend_allocation_failure(
        self,
        session: Session,
        message: dict | str,
        *,
        spec: PoolSpec,
        reason: str,
        turn_id: str | None,
        queue_id: int | None = None,
    ) -> None:
        failure_reason = f"{spec.allocation_failure_reason_prefix}: {reason}"
        self._update_session(
            session,
            status=Status.SUSPENDED,
            failure_reason=failure_reason,
            pane_id=None,
            **clear_turn_fields(),
        )
        if queue_id is not None:
            self.store.mark_queue_retryable(queue_id, last_error=failure_reason)
        else:
            self.store.append_to_queue(session.conversation_identity, message)
        if turn_id:
            cleanup_turn_context(turn_id)

    def _pool_for_session(self, session: Session) -> str:
        return pool_for_session(session)

    def _pool_spec_for_session(self, session: Session) -> PoolSpec | None:
        return find_pool_spec(extensions.EXTENSIONS, self._pool_for_session(session))

    def _primary_window_for_pool(self, pool_name: str) -> str:
        return windows_for_pool(pool_name)[0]

    def _workdir_for_session(self, session: Session) -> str:
        spec = self._pool_spec_for_session(session)
        if spec is not None:
            return spec.workdir
        self._ensure_worker_instruction_links(LIVE_WINDOW_WORKDIR)
        return LIVE_WINDOW_WORKDIR

    def _ensure_worker_instruction_links(self, workdir: str) -> None:
        workdir_path = Path(workdir)
        workdir_path.mkdir(parents=True, exist_ok=True)
        bridge_dir = Path(BRIDGE_DIR)
        instruction_sources = {
            "AGENTS.md": bridge_dir / "AGENTS.md",
            "IDENTITY.md": bridge_dir / "IDENTITY.md",
            "BRIDGE_PROTOCOL.md": bridge_dir / "BRIDGE_PROTOCOL.md",
        }
        for filename, instruction_source in instruction_sources.items():
            link_path = workdir_path / filename
            if link_path.is_symlink():
                if Path(link_path.readlink()) == instruction_source:
                    continue
                link_path.unlink()
            elif link_path.exists():
                raise RuntimeError(
                    "worker cwd instruction file already exists and is not managed by Slack Bridge: "
                    f"{link_path}"
                )
            link_path.symlink_to(instruction_source)

    def _ready_timeout_for_session(self, session: Session) -> int:
        spec = self._pool_spec_for_session(session)
        if spec is not None:
            return spec.ready_timeout
        return WORKER_READY_TIMEOUT

    def _build_codex_invocation(self, session: Session) -> str:
        args = [
            shlex.quote(config.CODEX_BIN),
            "--dangerously-bypass-approvals-and-sandbox",
            "--no-alt-screen",
            "-m",
            shlex.quote(config.CODEX_MODEL),
            "-c",
            shlex.quote(f"model_reasoning_effort={json.dumps(config.CODEX_REASONING_EFFORT)}"),
            "-c",
            "check_for_update_on_startup=false",
            "-C",
            shlex.quote(self._workdir_for_session(session)),
        ]
        return " ".join(args)

    def _build_claude_invocation(self) -> str:
        args = [
            shlex.quote(config.CLAUDE_BIN),
            "--dangerously-skip-permissions",
            "--model",
            shlex.quote(config.CLAUDE_MODEL),
            "--append-system-prompt",
            shlex.quote(AGENT_INSTRUCTION_PROMPT),
            "--add-dir",
            shlex.quote(BRIDGE_DIR),
        ]
        return " ".join(args)

    def _build_agent_invocation(self, session: Session) -> str:
        provider = config.AI_WORKER_PROVIDER
        if provider == "claude":
            cmd = self._build_claude_invocation()
        elif provider == "codex":
            cmd = self._build_codex_invocation(session)
        else:
            raise RuntimeError(f"unsupported AI_WORKER_PROVIDER: {provider!r}")
        spec = self._pool_spec_for_session(session)
        if spec is not None and spec.sandbox_profile is not None:
            profile = spec.sandbox_profile()
            return f"{SANDBOX_EXEC} -p {shlex.quote(profile)} {cmd}"
        return cmd

    def _allocate_pane(self, session: Session) -> tuple[str, str, str]:
        pool_name = self._pool_for_session(session)
        working_dir = self._workdir_for_session(session)
        allocation = self.tmux.allocate_pane(pool_name, working_dir)
        return allocation.window_name, allocation.pane_id, allocation.pool_name

    def _pane_claim_conflict(self, session: Session, pane_id: str) -> str | None:
        list_all = getattr(self.store, "list_all", None)
        if not callable(list_all):
            return None
        try:
            sessions = list_all()
        except Exception as exc:
            logger.warning("pane claim check failed for %s: %s", pane_id, exc)
            return None
        for existing in sessions:
            if getattr(existing, "conversation_identity", None) == session.conversation_identity:
                continue
            if getattr(existing, "status", None) not in PANE_CLAIM_STATUSES:
                continue
            if getattr(existing, "pane_id", None) == pane_id:
                return getattr(existing, "conversation_identity", "") or getattr(
                    existing, "thread_ts", ""
                )
        return None

    def _prepare_window(self, session: Session) -> str:
        current = self._load_current(session)
        if current.status in TERMINAL_STATUSES:
            raise RuntimeError(f"session terminal during recovery: {current.status.value}")
        current.window_name = (
            current.window_name
            or session.window_name
            or self._primary_window_for_pool(self._pool_for_session(current))
        )
        current = self._load_current(current)
        if current.status in TERMINAL_STATUSES:
            raise RuntimeError(f"session terminal during recovery: {current.status.value}")
        current.window_name = (
            current.window_name
            or session.window_name
            or self._primary_window_for_pool(self._pool_for_session(current))
        )
        self._update_session(current, status=Status.STARTING)
        window_name, pane_id, _pool_name = self._allocate_pane(current)
        conflict_identity = self._pane_claim_conflict(current, pane_id)
        if conflict_identity:
            raise PaneAllocationError(
                f"pane_id already claimed by recoverable session: {pane_id} {conflict_identity}"
            )
        self._update_session(current, window_name=window_name, pane_id=pane_id)
        mark_bridge_pane = getattr(self.tmux, "mark_bridge_pane", None)
        if callable(mark_bridge_pane):
            mark_bridge_pane(
                pane_id,
                conversation_identity=getattr(session, "conversation_identity", None),
                case_id=getattr(session, "case_id", None),
            )
        session.window_name = window_name
        session.pane_id = pane_id
        return pane_id

    def _dispatch_message(
        self,
        session: Session,
        pane_id: str,
        message: dict | str,
        *,
        turn_id: str,
        expected_status: Status = Status.READY,
        queue_id: int | None = None,
        recovery_mode: bool = False,
    ):
        ready_session = self._load_current(session)
        busy_session = self.store.transition(
            ready_session.conversation_identity,
            expected_status,
            Status.BUSY,
            updates=activate_turn_fields(
                ready_session,
                turn_id=turn_id,
                event_id=message.get("event_id") if isinstance(message, dict) else None,
                owner_user_id=(message.get("actor") or {}).get("user_id")
                if isinstance(message, dict)
                else ready_session.user_id,
                queue_id=queue_id,
            ),
        )
        assert busy_session is not None, (
            f"{expected_status.value}->BUSY transition failed: {ready_session.conversation_identity}"
        )
        if not pane_id:
            raise RuntimeError("pane_id is required for tmux dispatch")
        if not busy_session.window_name:
            raise RuntimeError("window_name is required for tmux dispatch")
        self._open_turn_attempt(busy_session, message)
        target = self.tmux.resolve_target(busy_session.window_name, pane_id)
        before_output = self._capture_dispatch_snapshot(target)
        turn_artifacts = write_turn_context(busy_session, turn_id=turn_id)
        payload = self._format_message_for_agent(
            busy_session,
            message,
            turn_id=turn_id,
            turn_artifacts=turn_artifacts,
            recovery_mode=recovery_mode,
        )
        self.tmux.send_text_and_enter(target, payload)
        if not self._confirm_turn_dispatch(target, turn_id, before_output):
            logger.warning(
                "turn dispatch could not be confirmed for %s turn_id=%s; continuing until completion timeout",
                busy_session.thread_ts,
                turn_id,
            )

    def _build_command(
        self,
        session: Session,
        *,
        pane_id: str | None = None,
    ) -> str:
        cmd = self._build_agent_invocation(session)
        effective_pane_id = pane_id or session.pane_id
        assert effective_pane_id, "pane_id is required before worker launch"
        assert session.worker_session_id, "worker_session_id is required before worker launch"
        env_parts = [
            f"cd {shlex.quote(self._workdir_for_session(session))}",
            f"export PATH=/opt/homebrew/bin:/usr/local/bin:{shlex.quote(BRIDGE_DIR)}:$PATH",
            f"export CASE_REPLY_CMD={shlex.quote(CASE_REPLY_CMD_PATH)}",
            "unset CC_TURN_ID",
            "unset BRIDGE_AUTH_TOKEN",
            f"export CC_TURN_CONTEXT_FILE={shlex.quote(turn_context_path(session.window_name, pane_id=effective_pane_id))}",
            f"export CC_WINDOW_NAME={shlex.quote(session.window_name)}",
            f"export SLACK_BRIDGE_WORKER_SESSION_ID={shlex.quote(session.worker_session_id)}",
            "export SLACK_BRIDGE_PANE=1",
            f"export BRIDGE_BASE_URL={shlex.quote(f'http://127.0.0.1:{config.HOOK_SERVER_PORT}')}",
        ]
        if config.SLACK_BRIDGE_INSTANCE:
            env_parts.append(
                f"export SLACK_BRIDGE_INSTANCE={shlex.quote(config.SLACK_BRIDGE_INSTANCE)}"
            )
        else:
            env_parts.append("unset SLACK_BRIDGE_INSTANCE")
        if session.conversation_identity:
            env_parts.append(
                f"export SLACK_BRIDGE_CONVERSATION_IDENTITY={shlex.quote(session.conversation_identity)}"
            )
        env_parts.append(f"export CC_PANE_ID={shlex.quote(effective_pane_id)}")
        if session.case_id:
            env_parts.append(f"export CC_CASE_ID={shlex.quote(session.case_id)}")
        return "; ".join(env_parts + [f"exec {cmd}"])

    def _start_command(self, session: Session, pane_id: str, *, turn_id: str):
        cmd = self._build_command(session, pane_id=pane_id)
        script_path = self._write_launch_script(cmd, turn_id=turn_id)
        self.tmux.send_text_and_enter(
            pane_id, f"exec {shlex.quote(config.SHELL_BIN)} {shlex.quote(str(script_path))}"
        )

    def _write_launch_script(self, command: str, *, turn_id: str) -> Path:
        launch_dir = Path(config.TMP_DIR) / "launch"
        launch_dir.mkdir(parents=True, exist_ok=True)
        script_path = launch_dir / f"{turn_id}.sh"
        prefix, separator, suffix = command.rpartition("; exec ")
        if separator:
            command = f"{prefix}; cleanup_launch_script; exec {suffix}"
        script_path.write_text(
            "#!/bin/sh\n"
            "set -e\n"
            '_SLACK_BRIDGE_LAUNCH_SCRIPT="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"\n'
            "cleanup_launch_script() {\n"
            '  rm -f -- "$_SLACK_BRIDGE_LAUNCH_SCRIPT"\n'
            "}\n"
            "trap cleanup_launch_script EXIT\n"
            f"{command}\n",
            encoding="utf-8",
        )
        script_path.chmod(0o700)
        return script_path

    def _update_session(self, session: Session, **updates) -> Session:
        updated = self._load_current(session)
        for key, value in updates.items():
            setattr(updated, key, value)
        if "status" not in updates:
            updated.last_activity_at = now_utc()
        self.store.save(updated)
        return updated

    def _capture_dispatch_snapshot(self, target: str) -> str:
        try:
            return self.tmux.capture_pane(target, lines=400)
        except Exception as exc:
            logger.debug("dispatch snapshot capture failed for %s: %s", target, exc)
            return ""

    def _write_ready_timeout_snapshot(
        self,
        session: Session,
        pane_id: str,
        *,
        turn_id: str,
        timeout: int,
    ) -> Path | None:
        try:
            output = self.tmux.capture_pane(pane_id, lines=400)
        except Exception as exc:
            output = f"[capture failed: {exc}]"
            logger.debug("ready timeout snapshot capture failed for %s: %s", pane_id, exc)
        metadata = {
            "conversation_identity": session.conversation_identity,
            "team_id": session.team_id,
            "channel_id": session.channel_id,
            "thread_ts": session.thread_ts,
            "window_name": session.window_name,
            "pane_id": pane_id,
            "turn_id": turn_id,
            "timeout_seconds": timeout,
            "captured_at": now_utc(),
        }
        try:
            READY_TIMEOUT_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
            path = READY_TIMEOUT_SNAPSHOT_DIR / f"{turn_id}.log"
            path.write_text(
                "=== metadata ===\n"
                f"{json.dumps(metadata, ensure_ascii=True, indent=2)}\n\n"
                "=== tmux capture ===\n"
                f"{output}",
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("ready timeout snapshot write failed for %s: %s", pane_id, exc)
            return None
        logger.warning(
            "ready timeout snapshot written for %s: %s", session.conversation_identity, path
        )
        return path

    def _confirm_turn_dispatch(self, target: str, turn_id: str, before_output: str = "") -> bool:
        markers = (f"[turn_id: {turn_id}]", f"- turn_id: {turn_id}")
        deadline = time.time() + 4.0
        before_ready = self._has_ready_prompt(before_output) if before_output else False
        while time.time() < deadline:
            try:
                output = self.tmux.capture_pane(target, lines=400)
            except Exception as exc:
                logger.debug("turn dispatch capture failed for %s: %s", target, exc)
                time.sleep(0.1)
                continue
            if any(marker in output for marker in markers):
                return True
            if "[Pasted text #" in output and "[Pasted text #" not in before_output:
                return True
            if (
                before_output
                and output != before_output
                and (not before_ready or not self._has_ready_prompt(output))
            ):
                return True
            time.sleep(0.1)
        return False

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
            payload=payload if payload != {} else None,
        )

    def _close_turn_attempt_on_failure(self, session: Session, *, turn_id: str | None) -> None:
        if self.state is None or not turn_id:
            return
        self.state.mark_turn_attempt_completed(
            turn_id,
            completed_at=now_utc(),
            completion_action="error",
        )

    def _fail_session(
        self,
        session: Session,
        pane_id: str | None,
        *,
        reason: str | None = None,
        turn_id: str | None = None,
        queue_id: int | None = None,
        status: Status = Status.ERROR,
    ) -> None:
        current = self._load_current(session)
        if current.status is Status.KILLED:
            logger.info(
                "skipping launch failure update; session already killed: %s",
                current.conversation_identity,
            )
            if turn_id:
                cleanup_turn_context(turn_id)
            return
        latest = self._update_session(
            session,
            status=status,
            failure_reason=reason,
            pane_id=None,
            **clear_turn_fields(),
        )
        if self.state is not None and latest.last_event_id:
            try:
                self.state.update_event(
                    latest.last_event_id,
                    status="failed",
                    conversation_identity=latest.conversation_identity,
                    failure_reason=reason or "session_error",
                )
            except Exception as exc:
                logger.warning(
                    "failed to mark launch event failed for %s: %s", latest.last_event_id, exc
                )
        if queue_id is not None:
            try:
                self.store.fail_queue_item(queue_id, reason=reason or "session_error")
            except Exception as exc:
                logger.warning("failed to mark launch queue item failed for %s: %s", queue_id, exc)
        self._close_turn_attempt_on_failure(latest, turn_id=turn_id)
        if turn_id:
            cleanup_turn_context(turn_id)
        if pane_id:
            try:
                self.tmux.kill_pane(pane_id)
            except Exception as exc:
                logger.debug("kill_pane failed during launch failure for %s: %s", pane_id, exc)

    def _format_message_for_agent(
        self,
        session: Session,
        message: dict | str,
        *,
        turn_id: str,
        turn_artifacts: TurnArtifacts,
        recovery_mode: bool = False,
    ) -> str:
        return build_worker_payload(
            session,
            message,
            turn_id=turn_id,
            reply_command_path=turn_artifacts.reply_command_path,
            fresh_worker=True,
            recovery_mode=recovery_mode,
        )

    def _timeout_message(self, session: Session) -> str:
        spec = self._pool_spec_for_session(session)
        if spec is not None and spec.ready_timeout_text:
            return spec.ready_timeout_text
        return CODEX_READY_TIMEOUT_TEXT

    def _error_message(self, session: Session, error: Exception) -> str:
        del error
        spec = self._pool_spec_for_session(session)
        if spec is not None and spec.launch_error_text:
            return spec.launch_error_text
        return CODEX_LAUNCH_ERROR_TEXT

    def _wait_for_ready(
        self, window_name: str, pane_id: str, timeout: int = WORKER_READY_TIMEOUT
    ) -> str:
        if not pane_id:
            raise RuntimeError("pane_id is required before waiting for worker readiness")
        target = pane_id
        last_permission_action_at = 0.0
        update_prompt_confirmed = False
        start = time.time()
        while time.time() - start < timeout:
            try:
                output = self.tmux.capture_pane(target)
                if self._has_ready_prompt(output):
                    return CODEX_READY
                if update_prompt_confirmed and self._pane_returned_after_update(target):
                    return CODEX_UPDATE_RESTART_REQUIRED
                if not update_prompt_confirmed and self._detect_update_prompt(output):
                    self._handle_update_prompt(target)
                    update_prompt_confirmed = True
                    time.sleep(2)
                    continue
                action = None if update_prompt_confirmed else self._detect_permission_dialog(output)
                if action and time.time() - last_permission_action_at >= 1:
                    self._handle_permission_dialog(target, action)
                    last_permission_action_at = time.time()
                    time.sleep(2)
                    continue
            except Exception as e:
                logger.debug("wait_for_ready capture failed for %s: %s", target, e)
                if update_prompt_confirmed and self._pane_returned_after_update(target):
                    return CODEX_UPDATE_RESTART_REQUIRED
            time.sleep(2)
        return CODEX_READY_TIMEOUT

    def _has_ready_prompt(self, output: str) -> bool:
        lines = [line.rstrip() for line in output.splitlines() if line.strip()]
        if not lines:
            return False
        return any(self._is_prompt_line(line) for line in lines[-5:])

    def _is_prompt_line(self, line: str) -> bool:
        normalized = line.replace("\u00a0", " ").strip()
        if not re.match(r"^[❯›>]", normalized):
            return False
        remainder = normalized[1:].lstrip()
        if not remainder:
            return True
        return re.match(r"^\d+\.", remainder) is None

    def _detect_update_prompt(self, output: str) -> bool:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        normalized = " ".join(lines).lower()
        return (
            "update available" in normalized
            and "update now" in normalized
            and ("skip" in normalized or "press enter to continue" in normalized)
        )

    def _handle_update_prompt(self, target: str) -> None:
        self.tmux.send_enter(target)

    def _pane_returned_after_update(self, target: str) -> bool:
        checker = getattr(self.tmux, "pane_is_shell_or_dead", None)
        if callable(checker):
            try:
                return bool(checker(target))
            except Exception as exc:
                logger.debug("update restart pane check failed for %s: %s", target, exc)
                return False
        exists = getattr(self.tmux, "pane_exists", None)
        if callable(exists):
            try:
                return not bool(exists(target))
            except Exception as exc:
                logger.debug("update restart pane existence check failed for %s: %s", target, exc)
                return False
        return False

    def _detect_permission_dialog(self, output: str) -> str | None:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        normalized = " ".join(lines).lower()
        has_trust_context = any(
            keyword in normalized for keyword in ("trust", "workspace", "folder")
        )
        has_exit_option = any("no" in line.lower() and "exit" in line.lower() for line in lines)
        has_menu_layout = any(re.match(r"^[❯>]\s*\d+\.", line) for line in lines)
        if not ((has_trust_context and has_exit_option) or (has_menu_layout and has_exit_option)):
            return None
        for line in lines:
            lower = line.lower()
            if "no" in lower and "exit" in lower and re.match(r"^[❯>]", line):
                return "select_yes_then_confirm"
        return "confirm"

    def _handle_permission_dialog(self, target: str, action: str):
        if action == "select_yes_then_confirm":
            self.tmux.send_keys(target, "Up", literal=False)
            time.sleep(0.1)
        self.tmux.send_enter(target)
