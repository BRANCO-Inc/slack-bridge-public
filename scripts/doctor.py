#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_MODULES = ("slack_bolt", "slack_sdk", "dotenv")
MINIMUM_PYTHON = (3, 14)
REQUIRED_POSIX_TOOLS = ("bash", "curl", "awk", "mktemp", "seq")
WINDOWS_RUNTIME_GUIDANCE = (
    "Native Windows Python is unsupported. Use WSL2 with "
    ".\\scripts\\windows.ps1 -Action doctor -Distro Ubuntu "
    "-ProjectPath /home/<user>/src/slack-bridge-public."
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def runtime_module():
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    return importlib.import_module("runtime_env")


def load_runtime_env() -> None:
    if importlib.util.find_spec("dotenv") is None:
        return
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    from scripts.run import load_env_files

    load_env_files()


def setting(name: str) -> str:
    return os.environ.get(name, "").strip()


def check_runtime_platform() -> Check:
    if sys.platform == "win32":
        return Check("linux_or_wsl_runtime", False, WINDOWS_RUNTIME_GUIDANCE)
    return Check("linux_or_wsl_runtime", True, sys.platform)


def check_python() -> Check:
    ok = sys.version_info[:2] >= MINIMUM_PYTHON
    version = ".".join(str(part) for part in sys.version_info[:3])
    return Check("python_version", ok, f"{version} (requires Python 3.14+)")


def venv_python_candidates() -> tuple[Path, ...]:
    return (PROJECT_ROOT / "venv" / "bin" / "python",)


def check_venv_python() -> Check:
    candidates = venv_python_candidates()
    ok = any(path.is_file() and os.access(path, os.X_OK) for path in candidates)
    return Check("venv_python_exists", ok, " | ".join(str(path) for path in candidates))


def check_running_from_venv() -> Check:
    venv_root = PROJECT_ROOT / "venv"
    ok = Path(sys.prefix).resolve() == venv_root.resolve()
    return Check("running_from_venv", ok, str(sys.prefix))


def check_requirements_import() -> Check:
    missing = [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]
    return Check(
        "requirements_import",
        not missing,
        "ok" if not missing else "missing: " + ", ".join(missing),
    )


def check_executable(name: str, env_name: str) -> Check:
    raw = setting(env_name)
    if raw:
        path = Path(raw).expanduser()
        ok = path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
        return Check(env_name.lower(), ok, str(path))
    candidate = shutil.which(name) or ""
    path = Path(candidate) if candidate else None
    ok = bool(path and path.is_absolute() and path.is_file() and os.access(path, os.X_OK))
    return Check(env_name.lower(), ok, candidate or "not found")


def check_profile() -> Check:
    try:
        profile = runtime_module().active_profile(os.environ)
    except RuntimeError as exc:
        return Check("slack_bridge_profile", False, str(exc))
    return Check("slack_bridge_profile", True, profile)


def check_ai_worker_provider() -> Check:
    try:
        provider = runtime_module().resolve_ai_worker_provider(os.environ)
    except RuntimeError as exc:
        return Check("ai_worker_provider", False, str(exc))
    return Check("ai_worker_provider", True, provider)


def check_worker_executable() -> Check:
    try:
        provider = runtime_module().resolve_ai_worker_provider(os.environ)
    except RuntimeError as exc:
        return Check("ai_worker_executable", False, str(exc))
    if provider == "claude":
        return check_executable("claude", "CLAUDE_BIN")
    if provider == "codex":
        return check_executable("codex", "CODEX_BIN")
    return Check("ai_worker_executable", False, f"invalid provider: {provider}")


def check_shell() -> Check:
    try:
        path = runtime_module().resolve_shell_bin_path(os.environ)
    except RuntimeError as exc:
        return Check("slack_bridge_shell_bin", False, str(exc))
    return Check("slack_bridge_shell_bin", True, path)


def check_data_root() -> Check:
    try:
        data_root = runtime_module().resolve_data_root_path(os.environ)
    except RuntimeError as exc:
        return Check("data_root_writable", False, str(exc))
    probe = data_root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    ok = probe.is_dir() and os.access(probe, os.W_OK | os.X_OK)
    return Check("data_root_writable", ok, str(data_root))


def check_posix_tool(name: str) -> Check:
    candidate = shutil.which(name) or ""
    path = Path(candidate) if candidate else None
    ok = bool(path and path.is_absolute() and path.is_file() and os.access(path, os.X_OK))
    return Check(f"posix_{name}", ok, candidate or "not found")


def check_hook_port() -> Check:
    try:
        runtime = runtime_module()
        instance = runtime.normalize_instance_name(os.environ.get("SLACK_BRIDGE_INSTANCE", ""))
        port = runtime.resolve_hook_server_port(os.environ, instance)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
    except RuntimeError as exc:
        return Check("hook_port_available", False, str(exc))
    except OSError as exc:
        return Check("hook_port_available", False, f"{port}: {exc}")
    return Check("hook_port_available", True, str(port))


def check_token_presence(name: str) -> Check:
    value = setting(name)
    ok = bool(value and not value.startswith("<"))
    return Check(name.lower() + "_exists", ok, "set" if ok else "missing")


def check_identity() -> Check:
    path = PROJECT_ROOT / "IDENTITY.md"
    return Check("identity_exists", path.is_file(), str(path))


def check_members() -> Check:
    raw = setting("SLACK_BRIDGE_MEMBERS_FILE") or "config/members.json"
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return Check("members_json_parseable", False, f"{path}: {exc}")
    except json.JSONDecodeError as exc:
        return Check(
            "members_json_parseable",
            False,
            f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}",
        )
    if not isinstance(data, dict):
        return Check(
            "members_json_parseable",
            False,
            f"{path}: top-level JSON value must be an object with a members list",
        )
    members = data.get("members")
    if not isinstance(members, list):
        return Check("members_json_parseable", False, f"{path}: members must be a list")
    for index, member in enumerate(members):
        if not isinstance(member, dict):
            return Check(
                "members_json_parseable",
                False,
                f"{path}: members[{index}] must be an object",
            )
        for field in ("slack_user_id", "cc_call"):
            if field not in member:
                return Check(
                    "members_json_parseable",
                    False,
                    f"{path}: members[{index}] is missing {field}",
                )
            if not isinstance(member[field], str):
                return Check(
                    "members_json_parseable",
                    False,
                    f"{path}: members[{index}].{field} must be a string",
                )
    return Check("members_json_parseable", True, str(path))


def check_manifest() -> Check:
    path = PROJECT_ROOT / "config" / "slack-app-manifest.yaml"
    return Check("manifest_generated", path.is_file(), str(path))


def run_checks() -> list[Check]:
    load_runtime_env()
    return [
        check_runtime_platform(),
        check_python(),
        check_venv_python(),
        check_running_from_venv(),
        check_requirements_import(),
        check_profile(),
        check_ai_worker_provider(),
        check_worker_executable(),
        check_executable("tmux", "TMUX_BIN"),
        check_shell(),
        *(check_posix_tool(name) for name in REQUIRED_POSIX_TOOLS),
        check_data_root(),
        check_hook_port(),
        check_token_presence("SLACK_BOT_TOKEN"),
        check_token_presence("SLACK_APP_TOKEN"),
        check_token_presence("SLACK_BRIDGE_AUTH_TOKEN"),
        check_identity(),
        check_members(),
        check_manifest(),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Slack Bridge public setup.")
    parser.add_argument("--json", action="store_true", help="print machine readable JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if sys.platform == "win32":
        check = check_runtime_platform()
        if args.json:
            print(json.dumps([check.__dict__], ensure_ascii=False, indent=2))
        else:
            print(f"fail\t{check.name}\t{check.detail}")
        return 2
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
