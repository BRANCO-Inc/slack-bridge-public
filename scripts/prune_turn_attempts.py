from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge_state import BridgeState  # noqa: E402  (sys.path 挿入が先行する単体スクリプト)
from config import STATE_DB_PATH  # noqa: E402


def prune_turn_attempts(
    *,
    db_path: str | None = None,
    older_than_days: int = 30,
    now: datetime | None = None,
) -> int:
    state = BridgeState(db_path or STATE_DB_PATH)
    return state.prune_old_turn_attempts(older_than_days=older_than_days, now=now)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prune old turn_attempts rows.")
    parser.add_argument("--days", type=int, default=30, help="retention in days")
    parser.add_argument("--db-path", default=None, help="override state DB path")
    args = parser.parse_args(argv)

    deleted = prune_turn_attempts(db_path=args.db_path, older_than_days=args.days)
    print(f"deleted={deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
