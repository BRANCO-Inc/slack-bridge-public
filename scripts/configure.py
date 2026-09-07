#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import warnings
from collections.abc import Mapping
from pathlib import Path

import setup as SETUP
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]

PROVIDERS = ("claude", "codex")
SETTING_KEYS = (
    "SLACK_BRIDGE_PROFILE",
    "SLACK_BRIDGE_HOOK_PORT",
    "SLACK_BRIDGE_COMPANY",
    "SLACK_BRIDGE_BOT_NAME",
    "AI_WORKER_PROVIDER",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
)
TOKEN_PATTERNS = {
    "SLACK_BOT_TOKEN": re.compile(r"xoxb-[A-Za-z0-9-]+\Z"),
    "SLACK_APP_TOKEN": re.compile(r"xapp-[A-Za-z0-9-]+\Z"),
}


class ConfigurationError(ValueError):
    pass


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        self.exit(2, f"{self.prog}: error: invalid arguments\n")


def env_file_values(path: Path) -> dict[str, str]:
    return {
        key: value
        for key, value in dotenv_values(path).items()
        if value is not None
    }


def effective_settings(
    project_root: Path = PROJECT_ROOT, environ: Mapping[str, str] | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for name in (".env", ".env.local"):
        for key, value in env_file_values(project_root / name).items():
            if key in SETTING_KEYS and key not in values:
                values[key] = value
                sources[key] = name
    for key, value in (os.environ if environ is None else environ).items():
        if key in SETTING_KEYS:
            values[key] = value
            sources[key] = "process environment"
    return values, sources


def validate_text(name: str, value: str | None, *, allow_empty: bool = False) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value and not allow_empty:
        raise ConfigurationError(f"{name} must not be blank")
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ConfigurationError(f"{name} must be one line")
    return value


def token_status(name: str, value: str | None) -> str:
    if not value or re.fullmatch(r"<[^>]+>", value):
        return "missing"
    return "valid" if TOKEN_PATTERNS[name].fullmatch(value) else "invalid"


def validate_token(name: str, value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ConfigurationError(f"{name} is not a valid Slack token")
    if token_status(name, value) != "valid":
        raise ConfigurationError(f"{name} is not a valid Slack token")
    return value


def validate_public_settings(settings: Mapping[str, str]) -> None:
    profile = settings.get("SLACK_BRIDGE_PROFILE", "public").strip().lower()
    if profile != "public":
        raise ConfigurationError("SLACK_BRIDGE_PROFILE must be public")

    provider = settings.get("AI_WORKER_PROVIDER", "claude").strip().lower()
    if provider not in PROVIDERS:
        raise ConfigurationError("AI_WORKER_PROVIDER must be claude or codex")

    port = settings.get("SLACK_BRIDGE_HOOK_PORT", "9111").strip()
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ConfigurationError("SLACK_BRIDGE_HOOK_PORT must be between 1 and 65535")


def render_env_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def update_env_text(text: str, updates: Mapping[str, str]) -> str:
    lines = text.splitlines(keepends=True)
    updated: list[str] = []
    seen: set[str] = set()
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=.*(?:\n)?\Z", line)
        if match and match.group(1) in updates:
            key = match.group(1)
            updated.append(f"{key}={render_env_value(updates[key])}\n")
            seen.add(key)
        else:
            updated.append(line)
    if updated and not updated[-1].endswith("\n"):
        updated[-1] += "\n"
    for key, value in updates.items():
        if key not in seen:
            updated.append(f"{key}={render_env_value(value)}\n")
    return "".join(updated)


def top_level_section(lines: list[str], heading: str) -> tuple[int, int]:
    start = next(
        (index for index, line in enumerate(lines) if line.rstrip("\n") == f"{heading}:"),
        None,
    )
    if start is None:
        raise ConfigurationError(f"manifest must include {heading}")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index] and not lines[index].startswith((" ", "\t", "\n"))
        ),
        len(lines),
    )
    return start, end


def set_manifest_field(
    lines: list[str], start: int, end: int, indent: int, field: str, value: str
) -> list[str]:
    prefix = " " * indent
    for index in range(start + 1, end):
        if re.match(rf"^{re.escape(prefix + field)}:\s*", lines[index]):
            lines[index] = f"{prefix}{field}: {render_env_value(value)}\n"
            return lines
    lines.insert(start + 1, f"{prefix}{field}: {render_env_value(value)}\n")
    return lines


def update_manifest_text(text: str, bot_name: str) -> str:
    lines = text.splitlines(keepends=True)
    display_start, display_end = top_level_section(lines, "display_information")
    lines = set_manifest_field(lines, display_start, display_end, 2, "name", bot_name)

    features_start, features_end = top_level_section(lines, "features")
    bot_start = next(
        (
            index
            for index in range(features_start + 1, features_end)
            if lines[index].rstrip("\n") == "  bot_user:"
        ),
        None,
    )
    if bot_start is None:
        raise ConfigurationError("manifest must include features.bot_user")
    bot_end = next(
        (
            index
            for index in range(bot_start + 1, features_end)
            if lines[index].strip()
            and len(lines[index]) - len(lines[index].lstrip(" ")) <= 2
        ),
        features_end,
    )
    lines = set_manifest_field(lines, bot_start, bot_end, 4, "display_name", bot_name)
    return "".join(lines)


def update_identity_text(text: str, bot_name: str | None, company: str | None) -> str:
    values = {"Name": bot_name, "Company": company}
    requested = {key: value for key, value in values.items() if value is not None}
    if not requested:
        return text

    lines = text.splitlines(keepends=True)
    start = next(
        (index for index, line in enumerate(lines) if line.rstrip("\n") == "## Identity"), None
    )
    if start is None:
        raise ConfigurationError("IDENTITY.md must include an Identity section")
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )
    found: set[str] = set()
    for index in range(start + 1, end):
        for key, value in requested.items():
            if re.match(rf"^- {key}:\s*", lines[index]):
                lines[index] = f"- {key}: {value}\n"
                found.add(key)
    insertion = [f"- {key}: {value}\n" for key, value in requested.items() if key not in found]
    if insertion:
        lines[start + 1 : start + 1] = insertion
    return "".join(lines)


def write_if_changed(path: Path, text: str, *, private: bool = False) -> bool:
    if private:
        path.chmod(0o600)
    if path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def collect_token_updates() -> dict[str, str]:
    if not sys.stdin.isatty():
        raise ConfigurationError(
            "token input requires an interactive terminal; run --tokens --apply in your own terminal"
        )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            bot_token = getpass.getpass("Slack Bot token (leave blank to keep current): ")
            app_token = getpass.getpass("Slack App token (leave blank to keep current): ")
    except getpass.GetPassWarning as error:
        raise ConfigurationError(
            "token input requires an echo-free local terminal; run --tokens --apply in your own terminal"
        ) from error
    bot_token = validate_token("SLACK_BOT_TOKEN", bot_token)
    app_token = validate_token("SLACK_APP_TOKEN", app_token)
    return {
        name: value
        for name, value in (("SLACK_BOT_TOKEN", bot_token), ("SLACK_APP_TOKEN", app_token))
        if value
    }


def collect_interactive_values(settings: Mapping[str, str]) -> dict[str, str]:
    def prompt(label: str, current: str) -> str | None:
        value = input(f"{label} [{current}]: ")
        return validate_text(label.lower(), value) if value.strip() else None

    company = prompt("Company", settings.get("SLACK_BRIDGE_COMPANY", ""))
    bot_name = prompt("Bot name", settings.get("SLACK_BRIDGE_BOT_NAME", "Slack Bridge Assistant"))
    provider = input(
        f"AI worker (claude/codex) [{settings.get('AI_WORKER_PROVIDER', 'claude')}]: "
    ).strip().lower()
    if provider and provider not in PROVIDERS:
        raise ConfigurationError("provider must be claude or codex")
    return {
        key: value
        for key, value in (
            ("SLACK_BRIDGE_COMPANY", company),
            ("SLACK_BRIDGE_BOT_NAME", bot_name),
            ("AI_WORKER_PROVIDER", provider or None),
        )
        if value is not None
    }


def ensure_no_process_conflict(updates: Mapping[str, str]) -> None:
    for key, value in updates.items():
        current = os.environ.get(key)
        if current is not None and current != value:
            raise ConfigurationError(
                f"{key} is set in the process environment; unset it before applying"
            )


def apply_configuration(
    brand_updates: Mapping[str, str], token_updates: Mapping[str, str], settings: Mapping[str, str]
) -> list[str]:
    env_path = PROJECT_ROOT / ".env"
    local_values = env_file_values(PROJECT_ROOT / ".env.local") if not env_path.exists() else {}
    changed = SETUP.setup()
    manifest_path = PROJECT_ROOT / "config" / "slack-app-manifest.yaml"
    identity_path = PROJECT_ROOT / "IDENTITY.md"
    env_values = env_file_values(env_path)

    bot_name = brand_updates.get(
        "SLACK_BRIDGE_BOT_NAME",
        settings.get("SLACK_BRIDGE_BOT_NAME", env_values.get("SLACK_BRIDGE_BOT_NAME", "")),
    )
    company = brand_updates.get(
        "SLACK_BRIDGE_COMPANY",
        settings.get("SLACK_BRIDGE_COMPANY", env_values.get("SLACK_BRIDGE_COMPANY", "")),
    )
    env_updates = {**local_values, **brand_updates, **token_updates}
    for key, value in (
        ("SLACK_BRIDGE_COMPANY", company),
        ("SLACK_BRIDGE_BOT_NAME", bot_name),
        ("AI_WORKER_PROVIDER", settings.get("AI_WORKER_PROVIDER")),
    ):
        if key not in env_values and value is not None:
            env_updates.setdefault(key, value)

    existing_env = env_path.read_text(encoding="utf-8")
    new_env = update_env_text(existing_env, env_updates) if env_updates else existing_env
    new_manifest = (
        update_manifest_text(manifest_path.read_text(encoding="utf-8"), bot_name)
        if bot_name
        else manifest_path.read_text(encoding="utf-8")
    )
    new_identity = update_identity_text(
        identity_path.read_text(encoding="utf-8"), bot_name or None, company
    )

    if write_if_changed(env_path, new_env, private=bool(token_updates)) and str(env_path) not in changed:
        changed.append(str(env_path))
    if write_if_changed(manifest_path, new_manifest) and str(manifest_path) not in changed:
        changed.append(str(manifest_path))
    if write_if_changed(identity_path, new_identity) and str(identity_path) not in changed:
        changed.append(str(identity_path))
    return changed


def print_preview(
    brand_updates: Mapping[str, str], settings: Mapping[str, str], sources: Mapping[str, str]
) -> None:
    print("preview: no files changed")
    initial_paths = (
        ".env",
        ".env.example",
        "IDENTITY.md",
        "config/members.json",
        "config/slack-app-manifest.yaml",
    )
    missing = [name for name in initial_paths if not (PROJECT_ROOT / name).exists()]
    if missing:
        print(f"- would create {len(missing)} files: {', '.join(missing)}")
    changed_keys = {key for key, value in brand_updates.items() if settings.get(key, "") != value}
    updated_paths: list[str] = [".env"] if changed_keys else []
    if "SLACK_BRIDGE_BOT_NAME" in changed_keys:
        updated_paths.extend(("config/slack-app-manifest.yaml", "IDENTITY.md"))
    elif "SLACK_BRIDGE_COMPANY" in changed_keys:
        updated_paths.append("IDENTITY.md")
    if updated_paths:
        print(f"- would update {len(updated_paths)} files: {', '.join(updated_paths)}")
    for key, value in brand_updates.items():
        current = settings.get(key, "")
        print(f"- {key}: {json.dumps(current, ensure_ascii=False)} -> {json.dumps(value, ensure_ascii=False)}")
    for key in ("SLACK_BRIDGE_COMPANY", "SLACK_BRIDGE_BOT_NAME", "AI_WORKER_PROVIDER"):
        source = sources.get(key, "default")
        print(f"- effective {key}: {source}")
    print(
        "- token status: "
        f"bot={token_status('SLACK_BOT_TOKEN', settings.get('SLACK_BOT_TOKEN'))}, "
        f"app={token_status('SLACK_APP_TOKEN', settings.get('SLACK_APP_TOKEN'))}"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = SafeArgumentParser(description="Configure a local Slack Bridge installation.")
    parser.add_argument("--company", metavar="TEXT")
    parser.add_argument("--bot-name", metavar="TEXT")
    parser.add_argument("--provider", choices=PROVIDERS)
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--tokens", action="store_true")
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if (args.interactive or args.tokens) and not args.apply:
            raise ConfigurationError("--interactive and --tokens require --apply")

        settings, sources = effective_settings(PROJECT_ROOT)
        brand_updates = {
            key: value
            for key, value in (
                (
                    "SLACK_BRIDGE_COMPANY",
                    validate_text("company", args.company, allow_empty=True),
                ),
                ("SLACK_BRIDGE_BOT_NAME", validate_text("bot name", args.bot_name)),
                ("AI_WORKER_PROVIDER", args.provider),
            )
            if value is not None
        }
        if args.interactive:
            brand_updates.update(collect_interactive_values(settings))

        candidate = {**settings, **brand_updates}
        validate_public_settings(candidate)
        ensure_no_process_conflict(brand_updates)
        if not args.apply:
            print_preview(brand_updates, settings, sources)
            return 0

        if args.interactive:
            changed = apply_configuration(brand_updates, {}, settings)
            print("Create and install the Slack app from config/slack-app-manifest.yaml.")
            input("Press Enter after the Slack app is ready to enter tokens (blank tokens keep current values): ")
            token_updates = collect_token_updates()
            ensure_no_process_conflict(token_updates)
            after_brand, _ = effective_settings(PROJECT_ROOT)
            for path in apply_configuration({}, token_updates, after_brand):
                if path not in changed:
                    changed.append(path)
        else:
            token_updates = collect_token_updates() if args.tokens else {}
            ensure_no_process_conflict(token_updates)
            changed = apply_configuration(brand_updates, token_updates, settings)
        print("configured:")
        for path in changed:
            print(f"- {path}")
        if not changed:
            print("- settings already match")
        if args.interactive:
            print("Sign in to the selected AI worker locally before starting Slack Bridge.")
        return 0
    except ConfigurationError as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
