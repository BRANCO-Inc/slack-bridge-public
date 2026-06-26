# ruff: noqa: E402
from __future__ import annotations

import argparse
import socket
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge_state import BridgeState
from config import (
    GENERAL_WINDOW_NAME,
    HOOK_SERVER_PORT,
    STATE_DB_PATH,
    TMUX_SESSION_NAME,
)
from pane_pool import all_window_names, legacy_window_names, pool_name_for_window, window_max_panes
from session import Status
from session_lifecycle import _coerce_now, reap_idle_sessions
from tmux_gateway import TmuxGateway

ACTIVE_SESSION_VALUES = frozenset(
    {
        Status.STARTING.value,
        Status.READY.value,
        Status.BUSY.value,
        Status.IDLE.value,
        Status.WAITING.value,
    }
)
STALE_SESSION_WINDOW = timedelta(hours=1)
STALE_SESSION_STATUSES = {Status.BUSY.value, Status.WAITING.value}
NON_ACTIONABLE_REPLY_ERRORS = frozenset(
    {
        "discarded_cancelled",
        "session_terminal",
        "stale_turn",
    }
)
DEFAULT_POOL_NAME = "general"
HOOK_PORT_HOST = "127.0.0.1"
HOOK_PORT_TIMEOUT_SECONDS = 0.2


def _bridge_owned_pane_ids(tmux: TmuxGateway, window_name: str) -> set[str]:
    list_pane_infos = getattr(tmux, "list_pane_infos", None)
    if not callable(list_pane_infos):
        return set()
    try:
        pane_infos = list_pane_infos(window_name)
    except Exception:
        return set()
    owned: set[str] = set()
    for pane in pane_infos:
        is_bridge_owned = getattr(pane, "is_bridge_owned", None)
        if callable(is_bridge_owned):
            if is_bridge_owned():
                owned.add(pane.pane_id)
            continue
        if getattr(pane, "bridge_owned", "") == "1":
            owned.add(pane.pane_id)
    return owned


def _human_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}{unit}"
            rounded = round(value, 1)
            if rounded.is_integer():
                return f"{int(rounded)}{unit}"
            return f"{rounded}{unit}"
        value /= 1024.0
    return f"{size}B"


def _query_scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    return int(row[0] or 0)


def _query_rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def _runtime_counter_value(conn: sqlite3.Connection, counter_name: str) -> int:
    row = conn.execute(
        "SELECT counter_value FROM runtime_counters WHERE counter_name = ?",
        (counter_name,),
    ).fetchone()
    if row is None:
        return 0
    return int(row["counter_value"] or 0)


def _normalize_window_name(window_name: str | None) -> str:
    value = (window_name or "").strip()
    return value or GENERAL_WINDOW_NAME


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except TypeError, ValueError:
        return default


def _is_tcp_port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=HOOK_PORT_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def _add_window_spec(
    specs: dict[str, dict],
    *,
    window_name: str,
    pool_name: str | None = None,
    capacity: int | None = None,
) -> None:
    window = _normalize_window_name(window_name)
    spec = specs.setdefault(
        window,
        {
            "pool": (pool_name or DEFAULT_POOL_NAME),
            "capacity": 0,
        },
    )
    if pool_name:
        spec["pool"] = pool_name
    if capacity is not None:
        spec["capacity"] = _coerce_int(capacity)


def _window_specs() -> dict[str, dict]:
    specs: dict[str, dict] = {}
    for window_name in all_window_names():
        _add_window_spec(
            specs,
            window_name=str(window_name),
            pool_name=str(pool_name_for_window(window_name)),
            capacity=_coerce_int(window_max_panes(window_name)),
        )
    for window_name in legacy_window_names():
        _add_window_spec(
            specs,
            window_name=str(window_name),
            pool_name=str(pool_name_for_window(window_name)),
            capacity=0,
        )
    return specs


def _window_ref(window_name: str, pane_id: str) -> str:
    return f"{window_name}:{pane_id}"


def _health_alert_reasons(report: dict) -> list[str]:
    reasons: list[str] = []
    if report.get("failed_24h", 0):
        reasons.append("failed_24h")
    if report.get("reply_posted_unknown_24h", 0):
        reasons.append("reply_posted_unknown_24h")
    if report.get("open_turn_attempts", 0):
        reasons.append("open_turn_attempts")
    if report.get("pool_full", 0):
        reasons.append("pool_full")
    if report.get("window_full", 0):
        reasons.append("window_full")
    if report.get("orphan_panes", 0):
        reasons.append("orphan")
    if report.get("unmanaged_panes", 0):
        reasons.append("unmanaged")
    if report.get("runtime_stale_sessions", report.get("stale_sessions", 0)):
        reasons.append("runtime_stale")
    if report.get("hook_port_listening") is False:
        reasons.append("hook_port_down")
    return reasons


def has_health_alert(report: dict) -> bool:
    return bool(_health_alert_reasons(report))


def _read_only_tmux_gateway() -> TmuxGateway:
    gateway = TmuxGateway.__new__(TmuxGateway)
    gateway.session_name = TMUX_SESSION_NAME
    return gateway


def collect_health(
    *,
    state: BridgeState | None = None,
    tmux: TmuxGateway | None = None,
    now: str | datetime | None = None,
    hook_port_checker=None,
) -> dict:
    tmux = tmux or _read_only_tmux_gateway()
    current = _coerce_now(now)
    db_path = Path(state.db_path if state is not None else STATE_DB_PATH)
    db_size = db_path.stat().st_size if db_path.exists() else 0
    failed_cutoff = (current - timedelta(hours=24)).isoformat()
    stale_cutoff = (current - STALE_SESSION_WINDOW).isoformat()
    active = 0
    active_by_window: dict[str, int] = {}
    failed_24h = 0
    event_failed_24h = 0
    event_queue_failed_24h = 0
    reply_failed_24h = 0
    reply_posted_unknown_24h = 0
    session_errors_24h = 0
    rejected_replies_24h = 0
    open_turn_attempts = 0
    event_failed_ids: list[str] = []
    event_queue_failed_ids: list[str] = []
    reply_posted_unknown_ids: list[str] = []
    open_turn_attempt_ids: list[str] = []
    stale_sessions = 0
    stale_by_window: dict[str, int] = {}
    stale_session_ids: list[str] = []
    session_pane_ids: set[str] = set()
    session_windows: set[str] = set()
    orphan_panes_reaped = 0
    orphaned_sessions = 0

    if db_path.exists():
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            active_rows = _query_rows(
                conn,
                f"""
                SELECT COALESCE(NULLIF(window_name, ''), ?) AS window_name, count(*) AS count
                FROM sessions
                WHERE status IN ({",".join("?" for _ in ACTIVE_SESSION_VALUES)})
                GROUP BY 1
                """,
                (GENERAL_WINDOW_NAME, *tuple(ACTIVE_SESSION_VALUES)),
            )
            active_by_window = {
                _normalize_window_name(row["window_name"]): int(row["count"] or 0)
                for row in active_rows
            }
            active = sum(active_by_window.values())
            reply_failed_24h = _query_scalar(
                conn,
                f"""
                SELECT count(*)
                FROM reply_ledger
                WHERE status = 'failed'
                  AND updated_at >= ?
                  AND (
                    last_error IS NULL
                    OR last_error NOT IN ({",".join("?" for _ in NON_ACTIONABLE_REPLY_ERRORS)})
                  )
                """,
                (failed_cutoff, *sorted(NON_ACTIONABLE_REPLY_ERRORS)),
            )
            reply_posted_unknown_rows = _query_rows(
                conn,
                """
                SELECT reply_request_id, last_error
                FROM reply_ledger
                WHERE status = 'posted_unknown'
                  AND updated_at >= ?
                ORDER BY updated_at ASC, reply_request_id ASC
                """,
                (failed_cutoff,),
            )
            reply_posted_unknown_24h = len(reply_posted_unknown_rows)
            reply_posted_unknown_ids = [
                row["reply_request_id"] + (f"({row['last_error']})" if row["last_error"] else "")
                for row in reply_posted_unknown_rows
            ]
            event_failed_rows = _query_rows(
                conn,
                """
                SELECT event_id, failure_reason
                FROM event_ledger
                WHERE status = 'failed'
                  AND last_updated_at >= ?
                ORDER BY last_updated_at ASC, event_id ASC
                """,
                (failed_cutoff,),
            )
            event_failed_24h = len(event_failed_rows)
            event_failed_ids = [
                row["event_id"] + (f"({row['failure_reason']})" if row["failure_reason"] else "")
                for row in event_failed_rows
            ]
            event_queue_failed_rows = _query_rows(
                conn,
                """
                SELECT queue_id, event_id, last_error
                FROM event_queue
                WHERE status = 'failed'
                  AND updated_at >= ?
                ORDER BY updated_at ASC, queue_id ASC
                """,
                (failed_cutoff,),
            )
            event_queue_failed_24h = len(event_queue_failed_rows)
            event_queue_failed_ids = [
                (row["event_id"] or f"queue:{row['queue_id']}")
                + (f"({row['last_error']})" if row["last_error"] else "")
                for row in event_queue_failed_rows
            ]
            open_turn_attempt_rows = _query_rows(
                conn,
                """
                SELECT turn_id, channel_id, thread_ts
                FROM turn_attempts
                WHERE completed_at IS NULL
                ORDER BY started_at ASC, turn_id ASC
                """,
            )
            open_turn_attempts = len(open_turn_attempt_rows)
            open_turn_attempt_ids = [
                f"{row['turn_id']}:{row['channel_id']}:{row['thread_ts']}"
                for row in open_turn_attempt_rows
            ]
            session_error_rows = _query_rows(
                conn,
                """
                SELECT channel_id, thread_ts, failure_reason
                FROM sessions
                WHERE status = 'error'
                  AND last_activity_at >= ?
                ORDER BY last_activity_at ASC, channel_id ASC, thread_ts ASC
                """,
                (failed_cutoff,),
            )
            session_errors_24h = len(session_error_rows)
            session_error_ids = [
                f"{row['channel_id']}:{row['thread_ts']}"
                + (f"({row['failure_reason']})" if row["failure_reason"] else "")
                for row in session_error_rows
            ]
            failed_24h = (
                reply_failed_24h
                + reply_posted_unknown_24h
                + session_errors_24h
                + event_failed_24h
                + event_queue_failed_24h
            )
            rejected_replies_24h = _query_scalar(
                conn,
                f"""
                SELECT count(*)
                FROM reply_ledger
                WHERE status = 'failed'
                  AND updated_at >= ?
                  AND last_error IN ({",".join("?" for _ in NON_ACTIONABLE_REPLY_ERRORS)})
                """,
                (failed_cutoff, *sorted(NON_ACTIONABLE_REPLY_ERRORS)),
            )
            stale_rows = _query_rows(
                conn,
                """
                SELECT COALESCE(NULLIF(window_name, ''), ?) AS window_name, channel_id, thread_ts
                FROM sessions
                WHERE status IN (?, ?)
                  AND last_activity_at < ?
                ORDER BY last_activity_at ASC, channel_id ASC, thread_ts ASC
                """,
                (GENERAL_WINDOW_NAME, *sorted(STALE_SESSION_STATUSES), stale_cutoff),
            )
            stale_sessions = len(stale_rows)
            stale_session_ids = [f"{row['channel_id']}:{row['thread_ts']}" for row in stale_rows]
            for row in stale_rows:
                window_name = _normalize_window_name(row["window_name"])
                stale_by_window[window_name] = stale_by_window.get(window_name, 0) + 1
            pane_rows = _query_rows(
                conn,
                """
                SELECT COALESCE(NULLIF(window_name, ''), ?) AS window_name, pane_id
                FROM sessions
                WHERE pane_id IS NOT NULL
                  AND pane_id != ''
                """,
                (GENERAL_WINDOW_NAME,),
            )
            for row in pane_rows:
                pane_id = row["pane_id"]
                if pane_id:
                    session_pane_ids.add(pane_id)
                    session_windows.add(_normalize_window_name(row["window_name"]))
            orphan_panes_reaped = _runtime_counter_value(conn, "orphan_panes_reaped")
            orphaned_sessions = _runtime_counter_value(conn, "orphaned_sessions")

    specs = _window_specs()
    for window_name in sorted(set(active_by_window) | set(stale_by_window) | session_windows):
        _add_window_spec(
            specs,
            window_name=window_name,
            pool_name=specs.get(window_name, {}).get("pool") or DEFAULT_POOL_NAME,
        )

    windows: dict[str, dict] = {}
    orphan_pane_ids: list[str] = []
    orphan_pane_refs: list[str] = []
    unmanaged_pane_ids: list[str] = []
    unmanaged_pane_refs: list[str] = []
    for window_name, spec in specs.items():
        try:
            live_panes = tmux.list_panes(window_name)
        except Exception:
            live_panes = []
        bridge_owned_panes = _bridge_owned_pane_ids(tmux, window_name)
        window_orphan_ids = [
            pane_id
            for pane_id in live_panes
            if pane_id in bridge_owned_panes and pane_id not in session_pane_ids
        ]
        window_unmanaged_ids = [
            pane_id
            for pane_id in live_panes
            if pane_id not in bridge_owned_panes and pane_id not in session_pane_ids
        ]
        orphan_pane_ids.extend(window_orphan_ids)
        orphan_pane_refs.extend(_window_ref(window_name, pane_id) for pane_id in window_orphan_ids)
        unmanaged_pane_ids.extend(window_unmanaged_ids)
        unmanaged_pane_refs.extend(
            _window_ref(window_name, pane_id) for pane_id in window_unmanaged_ids
        )

        capacity = _coerce_int(spec.get("capacity"))
        active_count = active_by_window.get(window_name, 0)
        orphan_count = len(window_orphan_ids)
        unmanaged_count = len(window_unmanaged_ids)
        stale_count = stale_by_window.get(window_name, 0)
        used = max(len(live_panes), active_count + orphan_count + unmanaged_count)
        windows[window_name] = {
            "pool": spec.get("pool") or DEFAULT_POOL_NAME,
            "capacity": capacity,
            "active": active_count,
            "orphan": orphan_count,
            "orphan_panes": orphan_count,
            "unmanaged": unmanaged_count,
            "unmanaged_panes": unmanaged_count,
            "stale": stale_count,
            "stale_sessions": stale_count,
            "live": len(live_panes),
            "full": bool(capacity and used >= capacity),
            "orphan_pane_ids": list(window_orphan_ids),
            "orphan_pane_refs": [
                _window_ref(window_name, pane_id) for pane_id in window_orphan_ids
            ],
            "unmanaged_pane_ids": list(window_unmanaged_ids),
            "unmanaged_pane_refs": [
                _window_ref(window_name, pane_id) for pane_id in window_unmanaged_ids
            ],
        }

    pools: dict[str, dict] = {}
    for window_name, metrics in windows.items():
        pool_name = metrics["pool"]
        pool = pools.setdefault(
            pool_name,
            {
                "capacity": 0,
                "active": 0,
                "orphan": 0,
                "unmanaged": 0,
                "stale": 0,
                "live": 0,
                "windows": [],
                "full": False,
                "orphan_pane_ids": [],
                "orphan_pane_refs": [],
                "unmanaged_pane_ids": [],
                "unmanaged_pane_refs": [],
            },
        )
        pool["capacity"] += metrics["capacity"]
        pool["active"] += metrics["active"]
        pool["orphan"] += metrics["orphan"]
        pool["unmanaged"] += metrics["unmanaged"]
        pool["stale"] += metrics["stale"]
        pool["live"] += metrics["live"]
        if (
            metrics["capacity"]
            or metrics["active"]
            or metrics["orphan"]
            or metrics["unmanaged"]
            or metrics["stale"]
            or metrics["live"]
        ):
            pool["windows"].append(window_name)
        pool["orphan_pane_ids"].extend(metrics["orphan_pane_ids"])
        pool["orphan_pane_refs"].extend(metrics["orphan_pane_refs"])
        pool["unmanaged_pane_ids"].extend(metrics["unmanaged_pane_ids"])
        pool["unmanaged_pane_refs"].extend(metrics["unmanaged_pane_refs"])

    for pool in pools.values():
        used = max(pool["live"], pool["active"] + pool["orphan"] + pool["unmanaged"])
        pool["full"] = bool(pool["capacity"] and used >= pool["capacity"])
        pool["orphan_panes"] = pool["orphan"]
        pool["unmanaged_panes"] = pool["unmanaged"]
        pool["stale_sessions"] = pool["stale"]

    orphan_panes = sum(window["orphan"] for window in windows.values())
    unmanaged_panes = sum(window["unmanaged"] for window in windows.values())
    pool_full_names = [pool_name for pool_name, metrics in pools.items() if metrics["full"]]
    window_full_names = [window_name for window_name, metrics in windows.items() if metrics["full"]]
    hook_port_checker = hook_port_checker or _is_tcp_port_listening
    hook_port_error = ""
    try:
        hook_port_listening = bool(hook_port_checker(HOOK_PORT_HOST, HOOK_SERVER_PORT))
    except Exception as exc:
        hook_port_listening = False
        hook_port_error = f"{type(exc).__name__}: {exc}"

    return {
        "checked_at": current.isoformat(),
        "active": active,
        "failed_24h": failed_24h,
        "event_failed_24h": event_failed_24h,
        "event_failed_ids": event_failed_ids,
        "event_queue_failed_24h": event_queue_failed_24h,
        "event_queue_failed_ids": event_queue_failed_ids,
        "reply_failed_24h": reply_failed_24h,
        "reply_posted_unknown_24h": reply_posted_unknown_24h,
        "reply_posted_unknown_ids": reply_posted_unknown_ids,
        "session_errors_24h": session_errors_24h,
        "session_error_ids": session_error_ids if db_path.exists() else [],
        "rejected_replies_24h": rejected_replies_24h,
        "open_turn_attempts": open_turn_attempts,
        "open_turn_attempt_ids": open_turn_attempt_ids,
        "orphan_panes": orphan_panes,
        "stale_sessions": stale_sessions,
        "db_size": db_size,
        "db_size_human": _human_bytes(db_size),
        "orphan_panes_reaped": orphan_panes_reaped,
        "orphaned_sessions": orphaned_sessions,
        "orphan_pane_ids": orphan_pane_ids,
        "orphan_pane_refs": orphan_pane_refs,
        "unmanaged_panes": unmanaged_panes,
        "unmanaged_pane_ids": unmanaged_pane_ids,
        "unmanaged_pane_refs": unmanaged_pane_refs,
        "stale_session_ids": stale_session_ids,
        "runtime_stale_sessions": stale_sessions,
        "runtime_stale_session_ids": stale_session_ids,
        "pool_full": len(pool_full_names),
        "pool_full_names": pool_full_names,
        "window_full": len(window_full_names),
        "window_full_names": window_full_names,
        "hook_port_host": HOOK_PORT_HOST,
        "hook_port": HOOK_SERVER_PORT,
        "hook_port_listening": hook_port_listening,
        "hook_port_error": hook_port_error,
        "pools": pools,
        "windows": windows,
    }


def format_health_report(report: dict, *, detail: bool = False) -> str:
    headline = report["checked_at"].replace("T", " ")[:16]
    prefix = "⚠️" if has_health_alert(report) else "✅"
    db_size_human = report.get("db_size_human") or _human_bytes(int(report.get("db_size", 0)))
    hook_port_summary = ""
    if "hook_port_listening" in report:
        hook_port_summary = f", hook_port_listening={str(report['hook_port_listening']).lower()}"
    summary = (
        f"{prefix} Slack Bridge 健全性 {headline}\n"
        f"active={report['active']}, failed_24h={report['failed_24h']}, "
        f"event_failed_24h={report.get('event_failed_24h', 0)}, "
        f"event_queue_failed_24h={report.get('event_queue_failed_24h', 0)}, "
        f"reply_posted_unknown_24h={report.get('reply_posted_unknown_24h', 0)}, "
        f"rejected_replies_24h={report.get('rejected_replies_24h', 0)}, orphan_panes={report['orphan_panes']}, "
        f"unmanaged_panes={report.get('unmanaged_panes', 0)}, stale_sessions={report['stale_sessions']}, "
        f"open_turn_attempts={report.get('open_turn_attempts', 0)}, "
        f"pool_full={report.get('pool_full', 0)}, window_full={report.get('window_full', 0)}{hook_port_summary}, "
        f"db_size={db_size_human}, "
        f"orphan_panes_reaped={report['orphan_panes_reaped']}, orphaned_sessions={report['orphaned_sessions']}"
    )
    if not detail:
        return summary

    lines = [summary, f"checked_at={report['checked_at']}"]
    alert_reasons = _health_alert_reasons(report)
    if alert_reasons:
        lines.append(f"alert_reasons={','.join(alert_reasons)}")
    if "hook_port_listening" in report:
        lines.append(
            f"hook_port={report.get('hook_port_host', HOOK_PORT_HOST)}:{report.get('hook_port', HOOK_SERVER_PORT)} "
            f"listening={str(report['hook_port_listening']).lower()}"
        )
    if report.get("hook_port_error"):
        lines.append(f"hook_port_error={report['hook_port_error']}")
    if report.get("pool_full_names"):
        lines.append(f"pool_full_names={','.join(report['pool_full_names'])}")
    if report.get("window_full_names"):
        lines.append(f"window_full_names={','.join(report['window_full_names'])}")
    orphan_pane_refs = report.get("orphan_pane_refs") or report["orphan_pane_ids"]
    if orphan_pane_refs:
        lines.append(f"orphan_pane_ids={','.join(orphan_pane_refs)}")
    if report.get("unmanaged_pane_ids"):
        lines.append(f"unmanaged_pane_ids={','.join(report['unmanaged_pane_ids'])}")
    if report.get("unmanaged_pane_refs"):
        lines.append(f"unmanaged_pane_refs={','.join(report['unmanaged_pane_refs'])}")
    if report["stale_session_ids"]:
        lines.append(f"stale_session_ids={','.join(report['stale_session_ids'])}")
    if report.get("session_error_ids"):
        lines.append(f"session_error_ids={','.join(report['session_error_ids'])}")
    if report.get("event_failed_ids"):
        lines.append(f"event_failed_ids={','.join(report['event_failed_ids'])}")
    if report.get("event_queue_failed_ids"):
        lines.append(f"event_queue_failed_ids={','.join(report['event_queue_failed_ids'])}")
    if report.get("reply_posted_unknown_ids"):
        lines.append(f"reply_posted_unknown_ids={','.join(report['reply_posted_unknown_ids'])}")
    if report.get("open_turn_attempt_ids"):
        lines.append(f"open_turn_attempt_ids={','.join(report['open_turn_attempt_ids'])}")
    for pool_name, metrics in report.get("pools", {}).items():
        lines.append(
            f"pool={pool_name} capacity={metrics['capacity']} active={metrics['active']} "
            f"orphan={metrics['orphan']} unmanaged={metrics['unmanaged']} stale={metrics['stale']} "
            f"full={str(metrics.get('full', False)).lower()} windows={','.join(metrics.get('windows', []))}"
        )
    for window_name, metrics in report.get("windows", {}).items():
        lines.append(
            f"window={window_name} pool={metrics['pool']} capacity={metrics['capacity']} active={metrics['active']} "
            f"orphan={metrics['orphan']} unmanaged={metrics['unmanaged']} stale={metrics['stale']} "
            f"full={str(metrics.get('full', False)).lower()}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect Slack Bridge health metrics.")
    parser.add_argument("--detail", action="store_true", help="print detail mode")
    parser.add_argument(
        "--reap",
        action="store_true",
        help="explicitly reap idle sessions before collecting health; default is read-only",
    )
    args = parser.parse_args(argv)

    reaped = 0
    if args.reap:
        reaped = reap_idle_sessions()
    report = collect_health()
    if args.reap:
        report["idle_sessions_reaped"] = reaped
    print(format_health_report(report, detail=args.detail))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
