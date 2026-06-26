from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Mapping, MutableMapping
from pathlib import Path

DEFAULT_HOOK_SERVER_PORT = 9111
DEFAULT_AI_WORKER_PROVIDER = "claude"
DEFAULT_CODEX_BIN = "/opt/homebrew/bin/codex"
DEFAULT_CLAUDE_BIN = "claude"
DEFAULT_SHELL_BIN = "/bin/zsh"
AI_WORKER_PROVIDERS = frozenset({"claude", "codex"})
PUBLIC_PROFILES = frozenset({"public", "core", "client"})
PRIVATE_PROFILES = frozenset()
DEFAULT_PROFILE = "public"
DEFAULT_DATA_ROOT = str(
    Path.home() / "Library" / "Application Support" / "slack-bridge"
)
DEFAULT_TMUX_SESSION_BASE = "slack-bridge"
LEGACY_TMUX_PREFIX = "legacy"
LEGACY_TMUX_SESSION_BASE = f"{LEGACY_TMUX_PREFIX}-code"
GENERAL_PANE_POOL_WINDOWS = ("worker", "worker-2")
OPTIONAL_EXTENSION_PANE_POOL_WINDOWS = ("worker-optional_extension", "worker-optional_extension-2")
LEGACY_GENERAL_PANE_POOL_WINDOWS = (LEGACY_TMUX_PREFIX, f"{LEGACY_TMUX_PREFIX}-2")
LEGACY_OPTIONAL_EXTENSION_PANE_POOL_WINDOWS = (
    f"{LEGACY_TMUX_PREFIX}-optional_extension",
    f"{LEGACY_TMUX_PREFIX}-optional_extension-2",
)
LEGACY_TO_CURRENT_WINDOW_NAMES = {
    legacy: current
    for legacy, current in zip(
        LEGACY_GENERAL_PANE_POOL_WINDOWS + LEGACY_OPTIONAL_EXTENSION_PANE_POOL_WINDOWS,
        GENERAL_PANE_POOL_WINDOWS + OPTIONAL_EXTENSION_PANE_POOL_WINDOWS,
        strict=True,
    )
}
INSTANCE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def active_profile(env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    value = (source.get("SLACK_BRIDGE_PROFILE", "") or DEFAULT_PROFILE).strip().lower()
    if value not in PUBLIC_PROFILES | PRIVATE_PROFILES:
        raise RuntimeError(
            "SLACK_BRIDGE_PROFILE must be one of "
            f"{sorted(PUBLIC_PROFILES | PRIVATE_PROFILES)} (received: {value!r})"
        )
    return value


def normalize_ai_worker_provider(raw_value: str) -> str:
    value = raw_value.strip().lower().replace("_", "-")
    if not value:
        return DEFAULT_AI_WORKER_PROVIDER
    aliases = {
        "claude-code": "claude",
        "claudecode": "claude",
        "anthropic": "claude",
        "codex-cli": "codex",
        "openai": "codex",
    }
    value = aliases.get(value, value)
    if value not in AI_WORKER_PROVIDERS:
        raise RuntimeError(
            "AI_WORKER_PROVIDER must be one of "
            f"{sorted(AI_WORKER_PROVIDERS)} (received: {raw_value!r})"
        )
    return value


def resolve_ai_worker_provider(env: Mapping[str, str]) -> str:
    for env_name in (
        "AI_WORKER_PROVIDER",
        "SLACK_BRIDGE_AI_PROVIDER",
        "SLACK_BRIDGE_WORKER_PROVIDER",
    ):
        raw_value = env.get(env_name, "")
        if raw_value.strip():
            return normalize_ai_worker_provider(raw_value)
    if active_profile(env) in PUBLIC_PROFILES:
        return "claude"
    return DEFAULT_AI_WORKER_PROVIDER


def private_profile_enabled(profile: str | None = None, env: Mapping[str, str] | None = None) -> bool:
    name = active_profile(env) if profile is None else profile.strip().lower()
    if name in PUBLIC_PROFILES:
        return False
    if name in PRIVATE_PROFILES:
        return True
    raise RuntimeError(
        "SLACK_BRIDGE_PROFILE must be one of "
        f"{sorted(PUBLIC_PROFILES | PRIVATE_PROFILES)} (received: {name!r})"
    )


def default_data_root_path(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    if active_profile(source) in PUBLIC_PROFILES:
        if os.name == "nt":
            base = source.get("LOCALAPPDATA", "").strip() or str(Path.home() / "AppData" / "Local")
            return Path(base) / "slack-bridge"
        home = Path(source.get("HOME", "")).expanduser() if source.get("HOME") else Path.home()
        return home / "Library" / "Application Support" / "slack-bridge"
    return Path(DEFAULT_DATA_ROOT)


def normalize_instance_name(raw_value: str) -> str:
    value = raw_value.strip().lower()
    if not value:
        return ""
    if not INSTANCE_NAME_PATTERN.fullmatch(value):
        raise RuntimeError(
            f"SLACK_BRIDGE_INSTANCE must match ^[a-z0-9][a-z0-9_-]*$ (received: {raw_value!r})"
        )
    return value


def write_normalized_instance(env: MutableMapping[str, str]) -> str:
    value = normalize_instance_name(env.get("SLACK_BRIDGE_INSTANCE", ""))
    if value:
        env["SLACK_BRIDGE_INSTANCE"] = value
    else:
        env.pop("SLACK_BRIDGE_INSTANCE", None)
    return value


def default_hook_server_port(instance: str) -> int:
    if not instance:
        return DEFAULT_HOOK_SERVER_PORT
    digest = hashlib.sha256(instance.encode("utf-8")).hexdigest()
    return 10000 + (int(digest[:8], 16) % 50000)


def resolve_hook_server_port(env: Mapping[str, str], instance: str) -> int:
    raw_value = env.get("SLACK_BRIDGE_HOOK_PORT", "").strip()
    if not raw_value:
        return default_hook_server_port(instance)
    try:
        port = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"SLACK_BRIDGE_HOOK_PORT must be an integer (received: {raw_value!r})"
        ) from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"SLACK_BRIDGE_HOOK_PORT must be between 1 and 65535 (received: {port})")
    return port


def resolve_data_root_path(env: Mapping[str, str]) -> Path:
    raw_value = env.get("SLACK_BRIDGE_DATA_ROOT", "").strip()
    path = Path(raw_value) if raw_value else default_data_root_path(env)
    if not path.is_absolute():
        raise RuntimeError(
            f"SLACK_BRIDGE_DATA_ROOT must be an absolute path (received: {raw_value!r})"
        )
    return path


def write_resolved_data_root(env: MutableMapping[str, str]) -> Path:
    path = resolve_data_root_path(env)
    env["SLACK_BRIDGE_DATA_ROOT"] = str(path)
    return path


def data_dir_for(data_root: Path, instance: str) -> Path:
    return data_root / "instances" / instance if instance else data_root


def default_tmux_session_name(instance: str) -> str:
    return f"{DEFAULT_TMUX_SESSION_BASE}-{instance}" if instance else DEFAULT_TMUX_SESSION_BASE


def legacy_tmux_session_names(instance: str) -> tuple[str, ...]:
    legacy_name = f"{LEGACY_TMUX_SESSION_BASE}-{instance}" if instance else LEGACY_TMUX_SESSION_BASE
    return (legacy_name,)


def default_live_cwd(data_dir: Path) -> Path:
    return data_dir / "workspaces" / "general"


def validate_executable_absolute_path(raw_value: str, *, env_name: str) -> str:
    if not raw_value:
        raise RuntimeError(f"{env_name} must be set to an executable absolute path.")
    path = Path(raw_value)
    if not path.is_absolute():
        raise RuntimeError(
            f"{env_name} must be an executable absolute path (received: {raw_value!r})."
        )
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(
            f"{env_name} must point to an executable file (received: {raw_value!r})."
        )
    return str(path)


def _which_absolute(name: str, env: Mapping[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    candidate = shutil.which(name, path=source.get("PATH")) or ""
    if not candidate:
        return ""
    path = Path(candidate)
    if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return ""


def resolve_codex_bin_path(
    env: Mapping[str, str],
    *,
    test_mode: bool = False,
    default_codex_bin: str = DEFAULT_CODEX_BIN,
) -> str:
    raw_value = env.get("CODEX_BIN", "").strip()
    if raw_value:
        return validate_executable_absolute_path(raw_value, env_name="CODEX_BIN")
    if test_mode:
        return "codex"
    if active_profile(env) in PUBLIC_PROFILES:
        candidate = _which_absolute("codex", env)
        if candidate:
            return candidate
        raise RuntimeError("CODEX_BIN must be set to an executable absolute path.")
    return validate_executable_absolute_path(default_codex_bin, env_name="CODEX_BIN")


def resolve_claude_bin_path(
    env: Mapping[str, str],
    *,
    test_mode: bool = False,
    default_claude_bin: str = DEFAULT_CLAUDE_BIN,
) -> str:
    raw_value = env.get("CLAUDE_BIN", "").strip()
    if raw_value:
        return validate_executable_absolute_path(raw_value, env_name="CLAUDE_BIN")
    if test_mode:
        return "claude"
    candidate = _which_absolute("claude", env)
    if candidate:
        return candidate
    if active_profile(env) in PUBLIC_PROFILES:
        raise RuntimeError("CLAUDE_BIN must be set to an executable absolute path.")
    return validate_executable_absolute_path(default_claude_bin, env_name="CLAUDE_BIN")


def resolve_worker_bin_path(
    env: Mapping[str, str],
    provider: str,
    *,
    test_mode: bool = False,
) -> str:
    normalized = normalize_ai_worker_provider(provider)
    if normalized == "claude":
        return resolve_claude_bin_path(env, test_mode=test_mode)
    return resolve_codex_bin_path(env, test_mode=test_mode)


def write_resolved_codex_bin(env: MutableMapping[str, str]) -> str:
    path = resolve_codex_bin_path(env, test_mode=False)
    env["CODEX_BIN"] = path
    return path


def write_resolved_worker_bin(env: MutableMapping[str, str]) -> str:
    provider = resolve_ai_worker_provider(env)
    path = resolve_worker_bin_path(env, provider, test_mode=False)
    env["AI_WORKER_PROVIDER"] = provider
    env["CLAUDE_BIN" if provider == "claude" else "CODEX_BIN"] = path
    return path


def resolve_tmux_bin_path(env: Mapping[str, str], *, test_mode: bool = False) -> str:
    raw_value = env.get("TMUX_BIN", "").strip()
    if raw_value:
        return validate_executable_absolute_path(raw_value, env_name="TMUX_BIN")
    if test_mode:
        return "tmux"
    candidate = _which_absolute("tmux", env)
    if candidate:
        return candidate
    raise RuntimeError("TMUX_BIN must be set to an executable absolute path.")


def resolve_shell_bin_path(env: Mapping[str, str], *, test_mode: bool = False) -> str:
    raw_value = (
        env.get("SLACK_BRIDGE_SHELL_BIN", "").strip()
        or env.get("SHELL_BIN", "").strip()
        or env.get("SHELL", "").strip()
    )
    if raw_value:
        return validate_executable_absolute_path(raw_value, env_name="SLACK_BRIDGE_SHELL_BIN")
    if test_mode:
        return "sh"
    for name in ("zsh", "bash", "sh"):
        candidate = _which_absolute(name, env)
        if candidate:
            return candidate
    return validate_executable_absolute_path(DEFAULT_SHELL_BIN, env_name="SLACK_BRIDGE_SHELL_BIN")
