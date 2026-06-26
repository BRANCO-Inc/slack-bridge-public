#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import secrets
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PROJECT_ROOT / "templates"


def copy_if_missing(source: Path, dest: Path, *, force: bool = False) -> bool:
    if dest.exists() and not force:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def env_text(auth_token: str | None = None) -> str:
    text = (TEMPLATES_DIR / "env.example").read_text(encoding="utf-8")
    if auth_token is None:
        return text
    return text.replace("<generate-with-setup>", auth_token)


def write_env_example(*, force: bool = False) -> bool:
    dest = PROJECT_ROOT / ".env.example"
    if dest.exists() and not force:
        return False
    dest.write_text(env_text(), encoding="utf-8")
    return True


def write_env(*, force: bool = False) -> bool:
    dest = PROJECT_ROOT / ".env"
    if dest.exists() and not force:
        return False
    dest.write_text(env_text(secrets.token_urlsafe(32)), encoding="utf-8")
    os.chmod(dest, 0o600)
    return True


def setup(force: bool = False) -> list[str]:
    written: list[str] = []
    targets = [
        (TEMPLATES_DIR / "IDENTITY.md.tmpl", PROJECT_ROOT / "IDENTITY.md"),
        (TEMPLATES_DIR / "members.json", PROJECT_ROOT / "config" / "members.json"),
        (
            TEMPLATES_DIR / "slack-app-manifest.yaml.tmpl",
            PROJECT_ROOT / "config" / "slack-app-manifest.yaml",
        ),
    ]
    for source, dest in targets:
        if copy_if_missing(source, dest, force=force):
            written.append(str(dest))
    if write_env_example(force=force):
        written.append(str(PROJECT_ROOT / ".env.example"))
    if write_env(force=force):
        written.append(str(PROJECT_ROOT / ".env"))
    return written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize Slack Bridge public files.")
    parser.add_argument("--force", action="store_true", help="overwrite generated files")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    written = setup(force=args.force)
    if written:
        print("created:")
        for path in written:
            print(f"- {path}")
    else:
        print("nothing changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
