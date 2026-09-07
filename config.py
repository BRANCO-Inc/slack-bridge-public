"""Slack Bridge public environment configuration."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Set
from pathlib import Path

from runtime_env import (
    GENERAL_PANE_POOL_WINDOWS as _GENERAL_PANE_POOL_WINDOWS,
)
from runtime_env import (
    LEGACY_GENERAL_PANE_POOL_WINDOWS as _LEGACY_GENERAL_PANE_POOL_WINDOWS,
)
from runtime_env import (
    LEGACY_TO_CURRENT_WINDOW_NAMES as _LEGACY_TO_CURRENT_WINDOW_NAMES,
)
from runtime_env import (
    active_profile,
    default_hook_server_port,
    default_live_cwd,
    default_tmux_session_name,
    legacy_tmux_session_names,
    normalize_instance_name,
    resolve_ai_worker_provider,
    resolve_claude_bin_path,
    resolve_codex_bin_path,
    resolve_data_root_path,
    resolve_hook_server_port,
    resolve_shell_bin_path,
    resolve_tmux_bin_path,
)
from slack_copy.renderer import render_message

TEST_MODE_ENV = "SLACK_BRIDGE_TEST_MODE"
TEST_MODE = os.environ.get(TEST_MODE_ENV, "").strip() == "1"
SLACK_BRIDGE_PROFILE = active_profile(os.environ)
SLACK_BRIDGE_INSTANCE = normalize_instance_name(os.environ.get("SLACK_BRIDGE_INSTANCE", ""))


def _instance_account_suffix() -> str:
    if not SLACK_BRIDGE_INSTANCE:
        return ""
    return f"_{SLACK_BRIDGE_INSTANCE.upper().replace('-', '_')}"


def _default_hook_server_port() -> int:
    return default_hook_server_port(SLACK_BRIDGE_INSTANCE)


def _resolve_hook_server_port() -> int:
    return resolve_hook_server_port(os.environ, SLACK_BRIDGE_INSTANCE)


def _resolve_health_channel_id() -> str:
    """Return health channel ID, or empty string if not configured.

    Health posting scripts should check this at runtime, not at import time.
    """
    return os.environ.get("SLACK_BRIDGE_HEALTH_CHANNEL_ID", "").strip()


def _resolve_codex_bin(
    *, env: dict[str, str] | os._Environ[str] = os.environ, test_mode: bool = TEST_MODE
) -> str:
    return resolve_codex_bin_path(env, test_mode=test_mode)


def _resolve_claude_bin(
    *, env: dict[str, str] | os._Environ[str] = os.environ, test_mode: bool = TEST_MODE
) -> str:
    return resolve_claude_bin_path(env, test_mode=test_mode)


def _resolve_ai_worker_provider(
    *,
    env: dict[str, str] | os._Environ[str] = os.environ,
) -> str:
    return resolve_ai_worker_provider(env)


def _resolve_tmux_bin(
    *, env: dict[str, str] | os._Environ[str] = os.environ, test_mode: bool = TEST_MODE
) -> str:
    return resolve_tmux_bin_path(env, test_mode=test_mode)


def _resolve_shell_bin(
    *, env: dict[str, str] | os._Environ[str] = os.environ, test_mode: bool = TEST_MODE
) -> str:
    return resolve_shell_bin_path(env, test_mode=test_mode)


def _initial_codex_bin(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    raw_value = source.get("CODEX_BIN", "").strip()
    if raw_value:
        return _resolve_codex_bin(env=dict(source), test_mode=TEST_MODE)
    return "codex"


def _initial_claude_bin(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    raw_value = source.get("CLAUDE_BIN", "").strip()
    if raw_value:
        return _resolve_claude_bin(env=dict(source), test_mode=TEST_MODE)
    return "claude"


def _initial_ai_worker_provider(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return resolve_ai_worker_provider(source)


def _initial_tmux_bin(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return source.get("TMUX_BIN", "").strip() or "tmux"


def _initial_shell_bin(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return (
        source.get("SLACK_BRIDGE_SHELL_BIN", "").strip()
        or source.get("SHELL_BIN", "").strip()
        or source.get("SHELL", "").strip()
        or "/bin/zsh"
    )


def _parse_csv_names(raw_value: str) -> tuple[str, ...]:
    names = []
    seen = set()
    for part in raw_value.split(","):
        name = part.strip()
        if not name or name in seen:
            continue
        names.append(name)
        seen.add(name)
    return tuple(names)


def _parse_non_negative_int_env(env_name: str, default: int) -> int:
    raw_value = os.environ.get(env_name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"{env_name} must be a non-negative integer (received: {raw_value!r})"
        ) from exc
    if value < 0:
        raise RuntimeError(f"{env_name} must be a non-negative integer (received: {value})")
    return value


def _resolve_tmux_session_name() -> str:
    return os.environ.get("SLACK_BRIDGE_TMUX_SESSION", "").strip() or default_tmux_session_name(
        SLACK_BRIDGE_INSTANCE
    )


def read_secret(account: str, *, optional: bool = False) -> str:
    effective_account = f"{account}{_instance_account_suffix()}"
    value = os.environ.get(effective_account, "").strip()
    if value:
        return value
    if optional:
        return ""
    raise RuntimeError(
        f"Public profile requires environment variable '{effective_account}' to be set."
    )


def _initial_secret(account: str) -> str:
    return os.environ.get(f"{account}{_instance_account_suffix()}", "").strip()


# Slack auth
SLACK_BOT_TOKEN = _initial_secret("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = _initial_secret("SLACK_APP_TOKEN")
SLACK_USER_TOKEN = SLACK_BOT_TOKEN
SLACK_BRIDGE_AUTH_TOKEN = _initial_secret("SLACK_BRIDGE_AUTH_TOKEN")


def resolve_runtime_secrets() -> None:
    """Resolve Slack credentials at entrypoint/runtime initialization time."""
    global SLACK_APP_TOKEN, SLACK_BOT_TOKEN, SLACK_BRIDGE_AUTH_TOKEN, SLACK_USER_TOKEN

    SLACK_BOT_TOKEN = read_secret("SLACK_BOT_TOKEN")
    SLACK_APP_TOKEN = read_secret("SLACK_APP_TOKEN")
    SLACK_USER_TOKEN = SLACK_BOT_TOKEN
    SLACK_BRIDGE_AUTH_TOKEN = read_secret("SLACK_BRIDGE_AUTH_TOKEN")


def resolve_runtime_paths() -> None:
    """Validate runtime executables after env files and launch context are loaded."""
    global AI_WORKER_PROVIDER, CLAUDE_BIN, CLAUDE_MODEL, CODEX_BIN, SHELL_BIN, TMUX_BIN

    AI_WORKER_PROVIDER = _resolve_ai_worker_provider()
    if AI_WORKER_PROVIDER == "claude":
        CLAUDE_BIN = _resolve_claude_bin()
    else:
        CODEX_BIN = _resolve_codex_bin()
    CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "").strip() or CLAUDE_MODEL
    TMUX_BIN = _resolve_tmux_bin()
    SHELL_BIN = _resolve_shell_bin()


# Derived values — resolved at runtime via auth.test API in slack_bridge.py
SLACK_BOT_USER_ID: str = ""
SLACK_TEAM_ID: str = ""

# tmux
TMUX_SESSION_NAME = _resolve_tmux_session_name()
LEGACY_TMUX_SESSION_NAMES = legacy_tmux_session_names(SLACK_BRIDGE_INSTANCE)
GENERAL_PANE_POOL_NAME = "general"
GENERAL_PANE_POOL_WINDOWS = _GENERAL_PANE_POOL_WINDOWS
LEGACY_GENERAL_PANE_POOL_WINDOWS = _LEGACY_GENERAL_PANE_POOL_WINDOWS
LEGACY_TO_CURRENT_WINDOW_NAMES = _LEGACY_TO_CURRENT_WINDOW_NAMES
PANE_POOL_WINDOW_MAX_PANES = 12
GENERAL_PANE_POOL_MAX_PANES = PANE_POOL_WINDOW_MAX_PANES * len(GENERAL_PANE_POOL_WINDOWS)
PANE_POOL_MAX_PANES = GENERAL_PANE_POOL_MAX_PANES
GENERAL_WINDOW_NAME = GENERAL_PANE_POOL_WINDOWS[0]
TMUX_COMMAND_TIMEOUT = 10
TMUX_SEND_TIMEOUT = 30
TMUX_PASTE_SETTLE_DELAY = 0.2
PANE_MIN_WIDTH = 120
PANE_MIN_HEIGHT = 20
TMUX_POOL_WINDOW_WIDTH = 488
TMUX_POOL_WINDOW_HEIGHT = 92


def _resolve_live_window_workdir(data_dir: str) -> str:
    override = os.environ.get("SLACK_BRIDGE_LIVE_CWD", "").strip()
    if override:
        return override
    return str(default_live_cwd(Path(data_dir)))


def _resolve_data_root(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    return str(resolve_data_root_path(source))


# Directories
BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = _resolve_data_root()
# Per-instance data root. Code stays in BRIDGE_DIR; mutable runtime data lives
# under DATA_ROOT so state/log/session/workspace files never mix with source.
DATA_DIR = (
    os.path.join(DATA_ROOT, "instances", SLACK_BRIDGE_INSTANCE)
    if SLACK_BRIDGE_INSTANCE
    else DATA_ROOT
)
RUNTIME_DIR = os.path.join(DATA_DIR, "runtime")
TMP_DIR = os.path.join(RUNTIME_DIR, "tmp")
SLACK_FILES_DIR = os.path.join(RUNTIME_DIR, "slack_files")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
STATE_DIR = os.path.join(DATA_DIR, "state")
STATE_DB_PATH = os.path.join(STATE_DIR, "bridge_state.sqlite3")
WORKSPACES_BASE = os.path.join(DATA_DIR, "workspaces")
TURN_CONTEXT_FILENAME = ".slack-bridge-turn.json"
AI_WORKER_PROVIDER = _initial_ai_worker_provider()
CLAUDE_BIN = _initial_claude_bin()
CODEX_BIN = _initial_codex_bin()
TMUX_BIN = _initial_tmux_bin()
SHELL_BIN = _initial_shell_bin()
CODEX_MODEL = "gpt-5.5"
CODEX_REASONING_EFFORT = "xhigh"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "").strip() or "sonnet"
LIVE_WINDOW_WORKDIR = _resolve_live_window_workdir(DATA_DIR)

# Limits and timings
MAX_CONCURRENT_SESSIONS = PANE_POOL_MAX_PANES
STALE_TIMEOUT = 3 * 60 * 60
HOOK_SERVER_PORT = _resolve_hook_server_port()
SLACK_POST_MAX_LEN = 3900
SLACK_API_MAX_RETRIES = 3
SLACK_FILE_DOWNLOAD_TIMEOUT_SECONDS = 30
SLACK_FILE_DOWNLOAD_MAX_BYTES = 25 * 1024 * 1024
WORKER_READY_TIMEOUT = 120
TURN_COMPLETION_TIMEOUT = 60 * 60
TERMINAL_SESSION_RETENTION = 24 * 60 * 60
INPUT_DETECT_INTERVAL = 3
INPUT_DETECT_STABLE_THRESHOLD = 2
INPUT_WAIT_TIMEOUT = 180 * 60
LATE_COMPLETION_WINDOW_SECONDS = 60
IDLE_TIMEOUT_HOURS = 3
FREE_INPUT_GRACE_PERIOD = 2 * 60


def _render_fixed_copy(key: str, context: Mapping[str, object] | None = None) -> str:
    return render_message(key, context)


def _normalize_copy_context(context: Mapping[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, value in context.items():
        text = "" if value is None else str(value).strip()
        normalized[key] = text or "未記載"
    return normalized


class _FixedCopyTemplate:
    def __init__(self, key: str) -> None:
        self.key = key

    def format(self, **context: object) -> str:
        return _render_fixed_copy(self.key, _normalize_copy_context(context))


DEFAULT_ACK_TEXT = _render_fixed_copy("ack")
SESSION_NOT_FOUND_TEXT = _render_fixed_copy("session_not_found")
SESSION_ERROR_TEXT = _render_fixed_copy("session_error")
UNKNOWN_STATUS_TEXT = _FixedCopyTemplate("unknown_status")
EMPTY_ANSWER_TEXT = _render_fixed_copy("empty_answer")
ANSWER_SEND_FAILED_TEXT = _render_fixed_copy("answer_send_failed")
STATE_UPDATE_FAILED_TEXT = _render_fixed_copy("state_update_failed")
QUEUE_APPEND_FAILED_TEXT = _render_fixed_copy("queue_append_failed")
QUEUE_SEND_FAILED_TEXT = _render_fixed_copy("queue_send_failed")
QUEUE_RETRY_TEXT = _render_fixed_copy("queue_retry")
SESSION_KILLED_TEXT = _render_fixed_copy("session_killed")
RECOVERY_PROGRESS_TEXT = _render_fixed_copy("recovery_progress")
PROCESS_EXITED_TEXT = _render_fixed_copy("process_exited")
SESSION_INTERRUPTED_TEXT = _render_fixed_copy("session_interrupted")
SESSION_ABNORMAL_EXIT_TEXT = _render_fixed_copy("session_abnormal_exit")
TURN_TIMEOUT_TEXT = _render_fixed_copy("turn_timeout")
WAIT_TIMEOUT_TEXT = _render_fixed_copy("wait_timeout")
RESUME_FAILED_TEXT = _render_fixed_copy("resume_failed")
CODEX_READY_TIMEOUT_TEXT = _render_fixed_copy("codex_ready_timeout")
CODEX_LAUNCH_ERROR_TEXT = _render_fixed_copy("codex_launch_error")
HEALTH_CHANNEL_ID = _resolve_health_channel_id()
EVENT_LEDGER_RETENTION_DAYS = 7
BOOTSTRAP_LEDGER_RETENTION_HOURS = 24
REPLY_CLAIM_TIMEOUT = 30

# Hooks
CASE_REPLY_CMD_PATH = os.path.join(BRIDGE_DIR, "case_reply.sh")
WORKER_REPLY_AUTH_WRAPPER_PATH = ""
NOTIFICATION_RATE_LIMIT_CHANNEL_IDS = frozenset(
    _parse_csv_names(
        os.environ.get(
            "SLACK_BRIDGE_NOTIFICATION_RATE_LIMIT_CHANNEL_IDS",
            "",
        )
    )
)
NOTIFICATION_DAILY_LIMIT = _parse_non_negative_int_env("SLACK_BRIDGE_NOTIFICATION_DAILY_LIMIT", 2)
SLACK_CONNECT_ALLOWED_CHANNEL_NAME_PREFIXES = _parse_csv_names(
    os.environ.get("SLACK_BRIDGE_CONNECT_ALLOWED_CHANNEL_NAME_PREFIXES", "")
)
AI_BOOT_REACTION_NAME = "ai-boot"
AI_BOOT_THREAD_CONTEXT_LIMIT = 200


# Users are loaded from SLACK_BRIDGE_MEMBERS_FILE or config/members.json.
def _members_json_path() -> Path:
    override = os.environ.get("SLACK_BRIDGE_MEMBERS_FILE", "").strip()
    if override:
        return Path(override)
    return Path(BRIDGE_DIR) / "config" / "members.json"


_MEMBERS_JSON = _members_json_path()


def _load_members() -> dict[str, str]:
    path = _MEMBERS_JSON
    if not path.exists():
        return {}
    with open(path) as f:
        data = json.load(f)
    return {m["slack_user_id"]: m["cc_call"] for m in data["members"]}


class _LazyCallNameMap(Mapping[str, str]):
    def __init__(self) -> None:
        self._value: dict[str, str] | None = None

    def _load(self) -> dict[str, str]:
        if self._value is None:
            self._value = _load_members()
        return self._value

    def __getitem__(self, key: str) -> str:
        return self._load()[key]

    def __iter__(self):
        return iter(self._load())

    def __len__(self) -> int:
        return len(self._load())

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._load().get(key, default)


class _LazyAdminUserIds(Set[str]):
    def __contains__(self, value: object) -> bool:
        return value in CALL_NAME_MAP

    def __iter__(self):
        return iter(CALL_NAME_MAP)

    def __len__(self) -> int:
        return len(CALL_NAME_MAP)


CALL_NAME_MAP = _LazyCallNameMap()
ADMIN_USER_IDS = _LazyAdminUserIds()
