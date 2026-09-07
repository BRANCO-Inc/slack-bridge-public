"""Create the private reply launcher used by worker turn wrappers."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

import config


def prepare_worker_reply_auth() -> Path:
    token = config.SLACK_BRIDGE_AUTH_TOKEN.strip()
    if not token:
        raise RuntimeError("SLACK_BRIDGE_AUTH_TOKEN must be resolved before worker launch")
    auth_dir = Path(config.TMP_DIR) / "reply-auth"
    auth_dir.mkdir(parents=True, exist_ok=True)
    auth_dir.chmod(0o700)
    path = auth_dir / "case-reply-auth.sh"
    temporary = auth_dir / f".{path.name}.{os.getpid()}"
    temporary.write_text(
        f'#!/bin/sh\nset -eu\nexport SLACK_BRIDGE_AUTH_TOKEN={shlex.quote(token)}\nexec "$@"\n',
        encoding="utf-8",
    )
    temporary.chmod(0o700)
    os.replace(temporary, path)
    path.chmod(0o700)
    config.WORKER_REPLY_AUTH_WRAPPER_PATH = str(path)
    return path
