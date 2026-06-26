#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "slack-bridge"
    if os.name == "nt"
    else Path.home() / "Library" / "Application Support" / "slack-bridge"
)
REQUIRED_MODULES = ("slack_bolt", "slack_sdk", "dotenv")
AI_WORKER_PROVIDERS = {"claude", "codex"}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def load_env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in (PROJECT_ROOT / ".env", PROJECT_ROOT / ".env.example"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if not value or value.startswith("#") or "=" not in value:
                continue
            key, raw = value.split("=", 1)
            values.setdefault(key.strip(), raw.strip())
    return values


def setting(name: str, env_file: dict[str, str]) -> str:
    return os.environ.get(name, "").strip() or env_file.get(name, "").strip()


def worker_provider(env_file: dict[str, str]) -> str:
    raw = (
        setting("AI_WORKER_PROVIDER", env_file)
        or setting("SLACK_BRIDGE_AI_PROVIDER", env_file)
        or setting("SLACK_BRIDGE_WORKER_PROVIDER", env_file)
        or "claude"
    )
    value = raw.strip().lower().replace("_", "-")
    aliases = {
        "claude-code": "claude",
        "claudecode": "claude",
        "anthropic": "claude",
        "codex-cli": "codex",
        "openai": "codex",
    }
    return aliases.get(value, value)


def check_python() -> Check:
    ok = sys.version_info >= (3, 10)
    return Check("python_version", ok, sys.version.split()[0])


def venv_python_candidates() -> tuple[Path, ...]:
    return (
        PROJECT_ROOT / "venv" / "bin" / "python",
        PROJECT_ROOT / "venv" / "Scripts" / "python.exe",
    )


def check_venv_python() -> Check:
    candidates = venv_python_candidates()
    ok = any(path.is_file() and os.access(path, os.X_OK) for path in candidates)
    return Check("venv_python_exists", ok, " | ".join(str(path) for path in candidates))


def check_running_from_venv() -> Check:
    executable = Path(sys.executable).resolve()
    candidates = [path.resolve() for path in venv_python_candidates() if path.exists()]
    ok = executable in candidates
    return Check("running_from_venv", ok, str(executable))


def check_requirements_import() -> Check:
    missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    return Check(
        "requirements_import",
        not missing,
        "ok" if not missing else "missing: " + ", ".join(missing),
    )


def check_executable(name: str, env_name: str, env_file: dict[str, str]) -> Check:
    raw = setting(env_name, env_file)
    if raw:
        path = Path(raw).expanduser()
        ok = path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
        return Check(env_name.lower(), ok, str(path))
    candidate = shutil.which(name) or ""
    path = Path(candidate) if candidate else None
    ok = bool(path and path.is_absolute() and path.is_file() and os.access(path, os.X_OK))
    return Check(env_name.lower(), ok, candidate or "not found")


def check_ai_worker_provider(env_file: dict[str, str]) -> Check:
    provider = worker_provider(env_file)
    ok = provider in AI_WORKER_PROVIDERS
    return Check(
        "ai_worker_provider",
        ok,
        provider if ok else f"invalid: {provider}",
    )


def check_worker_executable(env_file: dict[str, str]) -> Check:
    provider = worker_provider(env_file)
    if provider == "claude":
        return check_executable("claude", "CLAUDE_BIN", env_file)
    if provider == "codex":
        return check_executable("codex", "CODEX_BIN", env_file)
    return Check("ai_worker_executable", False, f"invalid provider: {provider}")


def check_shell(env_file: dict[str, str]) -> Check:
    raw = setting("SLACK_BRIDGE_SHELL_BIN", env_file) or setting("SHELL_BIN", env_file)
    if raw:
        path = Path(raw).expanduser()
        ok = path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
        return Check("slack_bridge_shell_bin", ok, str(path))
    for name in ("zsh", "bash", "sh"):
        candidate = shutil.which(name) or ""
        if candidate:
            path = Path(candidate)
            ok = path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
            if ok:
                return Check("slack_bridge_shell_bin", True, str(path))
    return Check("slack_bridge_shell_bin", False, "not found")


def check_data_root(env_file: dict[str, str]) -> Check:
    raw = setting("SLACK_BRIDGE_DATA_ROOT", env_file)
    data_root = Path(raw).expanduser() if raw else DEFAULT_DATA_ROOT
    if raw and not data_root.is_absolute():
        return Check("data_root_writable", False, f"not absolute: {raw}")
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=data_root, prefix=".doctor-", delete=True):
            pass
    except OSError as exc:
        return Check("data_root_writable", False, f"{data_root}: {exc}")
    return Check("data_root_writable", True, str(data_root))


def check_hook_port(env_file: dict[str, str]) -> Check:
    raw = setting("SLACK_BRIDGE_HOOK_PORT", env_file) or "9111"
    try:
        port = int(raw)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
    except OSError as exc:
        return Check("hook_port_available", False, f"{raw}: {exc}")
    except ValueError:
        return Check("hook_port_available", False, f"invalid: {raw}")
    return Check("hook_port_available", True, raw)


def check_token_presence(name: str, env_file: dict[str, str]) -> Check:
    value = setting(name, env_file)
    ok = bool(value and not value.startswith("<"))
    return Check(name.lower() + "_exists", ok, "set" if ok else "missing")


def check_identity() -> Check:
    path = PROJECT_ROOT / "IDENTITY.md"
    return Check("identity_exists", path.is_file(), str(path))


def check_members(env_file: dict[str, str]) -> Check:
    raw = setting("SLACK_BRIDGE_MEMBERS_FILE", env_file) or "config/members.json"
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return Check("members_json_parseable", False, f"{path}: {exc}")
    except json.JSONDecodeError as exc:
        return Check("members_json_parseable", False, f"{path}: {exc}")
    ok = isinstance(data.get("members"), list)
    return Check("members_json_parseable", ok, str(path))


def check_manifest() -> Check:
    path = PROJECT_ROOT / "config" / "slack-app-manifest.yaml"
    return Check("manifest_generated", path.is_file(), str(path))


def run_checks() -> list[Check]:
    env_file = load_env_example()
    return [
        check_python(),
        check_venv_python(),
        check_running_from_venv(),
        check_requirements_import(),
        check_ai_worker_provider(env_file),
        check_worker_executable(env_file),
        check_executable("tmux", "TMUX_BIN", env_file),
        check_shell(env_file),
        check_data_root(env_file),
        check_hook_port(env_file),
        check_token_presence("SLACK_BOT_TOKEN", env_file),
        check_token_presence("SLACK_APP_TOKEN", env_file),
        check_token_presence("SLACK_BRIDGE_AUTH_TOKEN", env_file),
        check_identity(),
        check_members(env_file),
        check_manifest(),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Slack Bridge public setup.")
    parser.add_argument("--json", action="store_true", help="print machine readable JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    checks = run_checks()
    if args.json:
        print(json.dumps([check.__dict__ for check in checks], ensure_ascii=False, indent=2))
    else:
        for check in checks:
            status = "ok" if check.ok else "fail"
            print(f"{status}\t{check.name}\t{check.detail}")
    return 0 if all(check.ok for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
