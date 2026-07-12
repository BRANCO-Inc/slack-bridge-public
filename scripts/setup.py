#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = PROJECT_ROOT / "templates"
OVERWRITE_CONFIRM_EXACT = "overwrite:slack-bridge-public:generated-files"


def copy_if_missing(source: Path, dest: Path, *, overwrite: bool = False) -> bool:
    if dest.exists() and not overwrite:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return True


def env_text(auth_token: str | None = None) -> str:
    text = (TEMPLATES_DIR / "env.example").read_text(encoding="utf-8")
    if auth_token is None:
        return text
    return text.replace("<generate-with-setup>", auth_token)


def write_env_example(*, overwrite: bool = False) -> bool:
    dest = PROJECT_ROOT / ".env.example"
    if dest.exists() and not overwrite:
        return False
    dest.write_text(env_text(), encoding="utf-8")
    return True


def write_env(*, overwrite: bool = False) -> bool:
    dest = PROJECT_ROOT / ".env"
    if dest.exists() and not overwrite:
        return False
    dest.write_text(env_text(secrets.token_urlsafe(32)), encoding="utf-8")
    os.chmod(dest, 0o600)
    return True


def setup(confirm: str | None = None) -> list[str]:
    if confirm is not None and confirm != OVERWRITE_CONFIRM_EXACT:
        raise ValueError(f"overwrite requires --confirm {OVERWRITE_CONFIRM_EXACT}")

    overwrite = confirm == OVERWRITE_CONFIRM_EXACT
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
        if copy_if_missing(source, dest, overwrite=overwrite):
            written.append(str(dest))
    if write_env_example(overwrite=overwrite):
        written.append(str(PROJECT_ROOT / ".env.example"))
    if write_env(overwrite=overwrite):
        written.append(str(PROJECT_ROOT / ".env"))
    return written


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize Slack Bridge public files.")
    parser.add_argument(
        "--confirm",
        metavar="EXACT",
        help=f"overwrite generated files only when EXACT is {OVERWRITE_CONFIRM_EXACT}",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        written = setup(confirm=args.confirm)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    if written:
        print("created:")
        for path in written:
            print(f"- {path}")
    else:
        print("nothing changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
