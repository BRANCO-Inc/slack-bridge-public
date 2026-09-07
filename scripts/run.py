#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MINIMUM_PYTHON = (3, 14)
WINDOWS_RUNTIME_GUIDANCE = (
    "Native Windows Python is unsupported. Use WSL2 with "
    ".\\scripts\\windows.ps1 -Action run -Distro Ubuntu "
    "-ProjectPath /home/<user>/src/slack-bridge-public."
)


def load_env_files() -> None:
    from dotenv import load_dotenv

    for name in (".env", ".env.local"):
        path = PROJECT_ROOT / name
        if path.is_file():
            load_dotenv(path, override=False)


def main() -> int:
    if sys.platform == "win32":
        print(WINDOWS_RUNTIME_GUIDANCE, file=sys.stderr)
        return 2

    if sys.version_info[:2] < MINIMUM_PYTHON:
        version = ".".join(str(part) for part in sys.version_info[:3])
        print(
            f"Slack Bridge requires Python 3.14 or later (received {version}).",
            file=sys.stderr,
        )
        return 2

    os.chdir(PROJECT_ROOT)
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    load_env_files()

    import slack_bridge

    slack_bridge.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
