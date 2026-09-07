"""tmuxセッション/ウィンドウ/ペイン操作のラッパー"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import uuid

import config
from bridge_logging import get_logger
from config import (
    BRIDGE_DIR,
    GENERAL_WINDOW_NAME,
    LEGACY_TO_CURRENT_WINDOW_NAMES,
    PANE_MIN_HEIGHT,
    PANE_MIN_WIDTH,
    PANE_POOL_MAX_PANES,
    SLACK_BRIDGE_INSTANCE,
    TMUX_COMMAND_TIMEOUT,
    TMUX_PASTE_SETTLE_DELAY,
    TMUX_POOL_WINDOW_HEIGHT,
    TMUX_POOL_WINDOW_WIDTH,
    TMUX_SEND_TIMEOUT,
    TMUX_SESSION_NAME,
)
from pane_pool import (
    PaneAllocation,
    all_window_names,
    normalize_pool_name,
    pool_definition,
    pool_definition_for_window,
    windows_for_pool,
)

logger = get_logger(__name__)


REUSABLE_SHELL_COMMANDS = frozenset({"bash", "fish", "sh", "zsh"})
_PANE_ALLOCATION_LOCK = threading.RLock()
BRIDGE_PANE_OWNED_OPTION = "@slack_bridge_owned"
BRIDGE_PANE_INSTANCE_OPTION = "@slack_bridge_instance"
BRIDGE_PANE_IDENTITY_OPTION = "@slack_bridge_identity"
BRIDGE_PANE_MARKER_VERSION_OPTION = "@slack_bridge_marker_version"
BRIDGE_PANE_MARKER_VERSION = "1"
BRIDGE_PANE_INSTANCE = SLACK_BRIDGE_INSTANCE or "main"
TEMP_SPLIT_MIN_WIDTH = 8
TEMP_SPLIT_MIN_HEIGHT = 4


class PaneAllocationError(RuntimeError):
    """Raised when a tmux pane cannot be allocated explicitly."""


class TmuxQueryError(RuntimeError):
    """Raised when tmux state cannot be queried reliably."""


class PaneGeometry:
    def __init__(self, pane_id: str, width: int, height: int):
        self.pane_id = pane_id
        self.width = width
        self.height = height

    @property
    def area(self) -> int:
        return self.width * self.height


class PaneInfo:
    def __init__(
        self,
        pane_id: str,
        current_command: str = "",
        title: str = "",
        active: str = "",
        dead: str = "",
        start_command: str = "",
        bridge_owned: str = "",
        bridge_instance: str = "",
        bridge_identity: str = "",
    ):
        self.pane_id = pane_id
        self.current_command = current_command
        self.title = title
        self.active = active
        self.dead = dead
        self.start_command = start_command
        self.bridge_owned = bridge_owned
        self.bridge_instance = bridge_instance
        self.bridge_identity = bridge_identity

    def is_bridge_owned(self, *, instance: str | None = None) -> bool:
        return self.bridge_owned == "1" and self.bridge_instance == (
            instance or BRIDGE_PANE_INSTANCE
        )


class TmuxGateway:
    def __init__(self, session_name: str | None = None):
        self.session_name = session_name or TMUX_SESSION_NAME
        self.ensure_session()
        self._migrate_legacy_windows()
        self._normalize_session_windows()
        for window_name in all_window_names():
            self.ensure_window(window_name)

    # --- 基本コマンド実行 ---

    def _run(self, args: list[str], check: bool = True, timeout: int | float | None = None) -> str:
        result = subprocess.run(
            [config.TMUX_BIN] + args,
            capture_output=True,
            text=True,
            timeout=timeout or TMUX_COMMAND_TIMEOUT,
        )
        if check and result.returncode != 0:
            raise RuntimeError(
                f"{config.TMUX_BIN} {' '.join(args)} failed: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def _session_exists(self) -> bool:
        result = subprocess.run(
            [config.TMUX_BIN, "has-session", "-t", self.session_name],
            capture_output=True,
            text=True,
            timeout=TMUX_COMMAND_TIMEOUT,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise TmuxQueryError(f"{config.TMUX_BIN} has-session failed: {result.stderr.strip()}")

    def ensure_session(self) -> None:
        if self._session_exists():
            return
        pane_id = self._run(
            [
                "new-session",
                "-d",
                "-s",
                self.session_name,
                "-n",
                GENERAL_WINDOW_NAME,
                "-c",
                BRIDGE_DIR,
                "-P",
                "-F",
                "#{pane_id}",
            ]
        )
        if not self._session_exists():
            raise TmuxQueryError(f"tmux session was not created: {self.session_name}")
        if not pane_id:
            raise TmuxQueryError("tmux new-session returned no pane_id")
        self.mark_bridge_pane(pane_id)

    # --- ウィンドウ操作 ---

    def _enable_session_window_renumbering(self):
        self._run(
            ["set-option", "-t", self.session_name, "renumber-windows", "on"],
            check=False,
        )

    def _renumber_windows(self):
        self._run(["move-window", "-r", "-t", self.session_name], check=False)

    def _normalize_session_windows(self):
        try:
            self._enable_session_window_renumbering()
            self._renumber_windows()
        except Exception as e:
            logger.debug("normalize_session_windows failed for %s: %s", self.session_name, e)

    def _list_window_names(self) -> list[str]:
        output = self._run(["list-windows", "-t", self.session_name, "-F", "#{window_name}"])
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _migrate_legacy_windows(self) -> None:
        existing = set(self._list_window_names())
        for legacy_name, current_name in LEGACY_TO_CURRENT_WINDOW_NAMES.items():
            if legacy_name not in existing or current_name in existing:
                continue
            self._run(
                ["rename-window", "-t", self._window_target(legacy_name), current_name],
                check=True,
            )
            existing.remove(legacy_name)
            existing.add(current_name)

    def ensure_window(self, window_name: str, working_dir: str | None = None) -> bool:
        """Ensure a tmux window exists and return True when it was created."""
        effective_working_dir = working_dir or BRIDGE_DIR
        with _PANE_ALLOCATION_LOCK:
            self._normalize_session_windows()
            created = False
            existing = self._list_window_names()
            if window_name not in existing:
                command = [
                    "new-window",
                    "-t",
                    self.session_name,
                    "-n",
                    window_name,
                    "-d",
                ]
                if effective_working_dir:
                    command.extend(["-c", effective_working_dir])
                command.extend(["-P", "-F", "#{pane_id}"])
                pane_id = self._run(command)
                if not pane_id:
                    raise TmuxQueryError("tmux new-window returned no pane_id")
                self.mark_bridge_pane(pane_id)
                self._normalize_session_windows()
                created = True
            self._ensure_pool_window_size(window_name)
            return created

    def _window_target(self, window_name: str) -> str:
        return f"{self.session_name}:{window_name}"

    def _ensure_pool_window_size(self, window_name: str) -> None:
        if window_name not in all_window_names():
            return
        width, height = self._target_pool_window_size(window_name)
        self._run(
            [
                "resize-window",
                "-t",
                self._window_target(window_name),
                "-x",
                str(width),
                "-y",
                str(height),
            ],
            check=False,
        )

    def _target_pool_window_size(self, window_name: str) -> tuple[int, int]:
        window_width, window_height = self._read_tmux_size(
            [
                "display-message",
                "-t",
                self._window_target(window_name),
                "-p",
                "#{window_width} #{window_height}",
            ]
        )
        return (
            max(TMUX_POOL_WINDOW_WIDTH, window_width or 0),
            max(TMUX_POOL_WINDOW_HEIGHT, window_height or 0),
        )

    def _read_tmux_size(self, args: list[str]) -> tuple[int, int]:
        output = self._run(args)
        parts = output.split()
        if len(parts) < 2:
            return 0, 0
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return 0, 0

    def _window_size(
        self, window_name: str, panes: list[PaneGeometry] | None = None
    ) -> tuple[int, int]:
        width, height = self._read_tmux_size(
            [
                "display-message",
                "-t",
                self._window_target(window_name),
                "-p",
                "#{window_width} #{window_height}",
            ]
        )
        if width and height:
            return width, height
        if panes:
            return max(pane.width for pane in panes), max(pane.height for pane in panes)
        if window_name in all_window_names():
            return self._target_pool_window_size(window_name)
        return TMUX_POOL_WINDOW_WIDTH, TMUX_POOL_WINDOW_HEIGHT

    def _list_pane_geometries(self, window_name: str) -> list[PaneGeometry]:
        output = self._run(
            [
                "list-panes",
                "-t",
                self._window_target(window_name),
                "-F",
                "#{pane_id} #{pane_width} #{pane_height}",
            ],
        )
        panes: list[PaneGeometry] = []
        for line in output.splitlines():
            parts = line.split()
            if len(parts) != 3:
                continue
            pane_id, width, height = parts
            try:
                panes.append(PaneGeometry(pane_id, int(width), int(height)))
            except ValueError:
                continue
        return panes

    def _window_pane_limit(self, window_name: str) -> int:
        definition = pool_definition_for_window(window_name)
        if window_name in all_window_names():
            return min(definition.window_max_panes, PANE_POOL_MAX_PANES)
        return PANE_POOL_MAX_PANES

    def _choose_split_target(
        self, window_name: str, *, max_panes: int | None = None
    ) -> tuple[str, str]:
        panes = self._list_pane_geometries(window_name)
        pane_limit = max_panes if max_panes is not None else self._window_pane_limit(window_name)
        if len(panes) >= pane_limit:
            raise PaneAllocationError(
                f"{window_name} pane limit reached: {len(panes)}/{pane_limit}"
            )
        next_count = len(panes) + 1
        if not self._layout_can_fit(window_name, next_count, panes=panes):
            width, height = self._window_size(window_name, panes=panes)
            raise PaneAllocationError(
                f"{window_name} cannot fit {next_count} panes "
                f"at min={PANE_MIN_WIDTH}x{PANE_MIN_HEIGHT} "
                f"(window={width}x{height})"
            )

        width, height = self._window_size(window_name, panes=panes)
        current_columns, current_rows = self._layout_grid(len(panes), width, height)
        next_columns, next_rows = self._layout_grid(next_count, width, height)
        target_cell_width, target_cell_height = self._layout_cell_size(
            next_columns,
            next_rows,
            width,
            height,
        )
        preferred_axis = None
        if next_columns > current_columns:
            preferred_axis = "horizontal"
        elif next_rows > current_rows:
            preferred_axis = "vertical"

        candidates: list[tuple[float, int, PaneGeometry, str]] = []
        for pane in panes:
            axis_score = self._split_axis_score(
                pane,
                preferred_axis=preferred_axis,
                target_cell_width=target_cell_width,
                target_cell_height=target_cell_height,
            )
            if axis_score is None:
                continue
            score, axis = axis_score
            candidates.append((score, pane.area, pane, axis))

        if not candidates:
            raise PaneAllocationError(
                f"no splittable pane in {window_name} "
                f"(temporary min={TEMP_SPLIT_MIN_WIDTH}x{TEMP_SPLIT_MIN_HEIGHT}, panes={len(panes)})"
            )

        _, _, pane, axis = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
        return pane.pane_id, axis

    def _layout_grid(self, pane_count: int, width: int, height: int) -> tuple[int, int]:
        if pane_count <= 1:
            return 1, 1
        columns = 1
        rows = 1
        while columns * rows < pane_count:
            if columns == rows:
                if width >= height:
                    columns += 1
                else:
                    rows += 1
            elif columns < rows:
                columns += 1
            else:
                rows += 1
        return columns, rows

    def _layout_cell_size(
        self, columns: int, rows: int, width: int, height: int
    ) -> tuple[int, int]:
        return (
            (width - max(0, columns - 1)) // columns,
            (height - max(0, rows - 1)) // rows,
        )

    def _split_layout_extent(self, total: int, count: int) -> list[int]:
        usable = total - max(0, count - 1)
        base = usable // count
        remainder = usable % count
        return [base + (1 if index < remainder else 0) for index in range(count)]

    def _layout_checksum(self, layout: str) -> int:
        # tmux validates custom select-layout strings with this 16-bit checksum.
        checksum = 0
        for character in layout:
            checksum = (checksum >> 1) + ((checksum & 1) << 15)
            checksum += ord(character)
            checksum &= 0xFFFF
        return checksum

    def _grid_layout(self, pane_count: int, width: int, height: int) -> str | None:
        if pane_count <= 1 or width <= 0 or height <= 0:
            return None
        columns, rows = self._layout_grid(pane_count, width, height)
        row_counts = [columns] * (pane_count // columns)
        if pane_count % columns:
            row_counts.append(pane_count % columns)
        rows = len(row_counts)
        row_heights = self._split_layout_extent(height, rows)
        pane_id = 0
        row_cells: list[str] = []
        y_offset = 0
        for row_count, row_height in zip(row_counts, row_heights, strict=True):
            column_widths = self._split_layout_extent(width, row_count)
            x_offset = 0
            leaves: list[str] = []
            for column_width in column_widths:
                leaves.append(f"{column_width}x{row_height},{x_offset},{y_offset},{pane_id}")
                pane_id += 1
                x_offset += column_width + 1
            if row_count == 1:
                row_cells.append(leaves[0])
            else:
                row_cells.append(
                    f"{width}x{row_height},0,{y_offset}" + "{" + ",".join(leaves) + "}"
                )
            y_offset += row_height + 1
        if len(row_cells) == 1:
            body = row_cells[0]
        else:
            body = f"{width}x{height},0,0" + "[" + ",".join(row_cells) + "]"
        return f"{self._layout_checksum(body):04x},{body}"

    def _layout_can_fit(
        self,
        window_name: str,
        pane_count: int,
        *,
        panes: list[PaneGeometry] | None = None,
    ) -> bool:
        width, height = self._window_size(window_name, panes=panes)
        if width <= 0 or height <= 0:
            return False
        columns, rows = self._layout_grid(pane_count, width, height)
        cell_width, cell_height = self._layout_cell_size(columns, rows, width, height)
        return cell_width >= PANE_MIN_WIDTH and cell_height >= PANE_MIN_HEIGHT

    def _split_axis_score(
        self,
        pane: PaneGeometry,
        *,
        preferred_axis: str | None,
        target_cell_width: int,
        target_cell_height: int,
    ) -> tuple[float, str] | None:
        scores: list[tuple[float, str]] = []
        if (pane.width - 1) // 2 >= TEMP_SPLIT_MIN_WIDTH:
            score = pane.width / max(1, target_cell_width)
            if preferred_axis == "horizontal":
                score += 2.0
            scores.append((score, "horizontal"))
        if (pane.height - 1) // 2 >= TEMP_SPLIT_MIN_HEIGHT:
            score = pane.height / max(1, target_cell_height)
            if preferred_axis == "vertical":
                score += 2.0
            scores.append((score, "vertical"))
        if not scores:
            return None
        return max(scores, key=lambda candidate: candidate[0])

    def _rebalance_window_layout(self, window_name: str) -> None:
        pane_count = len(self.list_panes(window_name))
        width, height = self._window_size(window_name)
        layout = self._grid_layout(pane_count, width, height)
        if not layout:
            return
        self._run(["select-layout", "-t", self._window_target(window_name), layout], check=False)

    def _reusable_shell_pane(self, window_name: str, *, allow_unmarked: bool = False) -> str | None:
        panes = self.list_pane_infos(window_name)
        if len(panes) != 1:
            return None
        pane = panes[0]
        if pane.current_command not in REUSABLE_SHELL_COMMANDS:
            return None
        if pane.is_bridge_owned() or allow_unmarked:
            return pane.pane_id
        return None

    def new_pane(
        self,
        target_window: str = GENERAL_WINDOW_NAME,
        working_dir: str | None = None,
        *,
        reuse_unmarked_shell: bool = False,
    ) -> str:
        """既存 window に新しい pane を作成して pane_id を返す"""
        with _PANE_ALLOCATION_LOCK:
            target_window = target_window or GENERAL_WINDOW_NAME
            created_window = self.ensure_window(target_window, working_dir)
            self._normalize_session_windows()
            reusable_pane = self._reusable_shell_pane(
                target_window,
                allow_unmarked=reuse_unmarked_shell or created_window,
            )
            if reusable_pane:
                return reusable_pane
            self._rebalance_window_layout(target_window)
            target_pane, axis = self._choose_split_target(target_window)
            command = ["split-window"]
            if axis == "horizontal":
                command.append("-h")
            command.extend(["-t", target_pane, "-P", "-F", "#{pane_id}"])
            if working_dir:
                command.extend(["-c", working_dir])
            try:
                pane_id = self._run(command)
            except RuntimeError as exc:
                raise PaneAllocationError(f"pane allocation failed: {exc}") from exc
            if not pane_id:
                raise PaneAllocationError("tmux split-window returned no pane_id")
            self._normalize_session_windows()
            self._rebalance_window_layout(target_window)
            return pane_id

    def _pool_pane_count(self, pool_name: str) -> int:
        return sum(len(self.list_panes(window_name)) for window_name in windows_for_pool(pool_name))

    def allocate_pane(
        self, pool_name: str | None = None, working_dir: str | None = None
    ) -> PaneAllocation:
        """Allocate a pane from the named pool and return its window and pane IDs."""
        normalized_pool = normalize_pool_name(pool_name)
        definition = pool_definition(normalized_pool)
        errors: list[str] = []
        with _PANE_ALLOCATION_LOCK:
            if self._pool_pane_count(normalized_pool) >= definition.total_max_panes:
                raise PaneAllocationError(
                    f"{normalized_pool} pool limit reached: "
                    f"{definition.total_max_panes}/{definition.total_max_panes}"
                )
            for window_name in definition.window_names:
                try:
                    pane_id = self.new_pane(target_window=window_name, working_dir=working_dir)
                    return PaneAllocation(
                        pool_name=normalized_pool,
                        window_name=window_name,
                        pane_id=pane_id,
                    )
                except PaneAllocationError as exc:
                    errors.append(f"{window_name}: {exc}")
            detail = "; ".join(errors) if errors else "no windows configured"
            raise PaneAllocationError(f"{normalized_pool} pool allocation failed: {detail}")

    def mark_bridge_pane(
        self,
        pane_id: str,
        *,
        conversation_identity: str | None = None,
        case_id: str | None = None,
    ) -> None:
        """Mark a pane as owned by this Slack Bridge instance."""
        option_values = {
            BRIDGE_PANE_OWNED_OPTION: "1",
            BRIDGE_PANE_INSTANCE_OPTION: BRIDGE_PANE_INSTANCE,
            BRIDGE_PANE_IDENTITY_OPTION: conversation_identity or "",
            BRIDGE_PANE_MARKER_VERSION_OPTION: BRIDGE_PANE_MARKER_VERSION,
        }
        for option, value in option_values.items():
            self._run(["set-option", "-p", "-t", pane_id, option, value])
        title_subject = case_id or conversation_identity or pane_id
        self._run(
            [
                "select-pane",
                "-t",
                pane_id,
                "-T",
                f"Slack Bridge {BRIDGE_PANE_INSTANCE}: {title_subject}"[:120],
            ],
            check=True,
        )

    def _pane_window_and_ownership(self, pane_id: str) -> tuple[str, str, str]:
        output = self._run(
            [
                "display-message",
                "-t",
                pane_id,
                "-p",
                "\t".join(
                    [
                        "#{window_name}",
                        f"#{{{BRIDGE_PANE_OWNED_OPTION}}}",
                        f"#{{{BRIDGE_PANE_INSTANCE_OPTION}}}",
                    ]
                ),
            ],
        )
        parts = output.split("\t")
        if len(parts) < 3:
            parts.extend([""] * (3 - len(parts)))
        return parts[0].strip(), parts[1].strip(), parts[2].strip()

    def kill_pane(self, pane_id: str) -> bool:
        """ペインを削除し、所属ウィンドウのレイアウトを再調整"""
        window_name, bridge_owned, bridge_instance = self._pane_window_and_ownership(pane_id)
        if bridge_owned != "1" or bridge_instance != BRIDGE_PANE_INSTANCE:
            logger.debug("skip unmanaged pane kill for %s", pane_id)
            return False
        self._run(["kill-pane", "-t", pane_id])
        if self.pane_exists(pane_id):
            return False
        if window_name:
            self.ensure_window(window_name)
            self._rebalance_window_layout(window_name)
        return True

    def list_panes(self, window_name: str) -> list[str]:
        """ウィンドウ内のペインID一覧を取得"""
        output = self._run(
            ["list-panes", "-t", self._window_target(window_name), "-F", "#{pane_id}"]
        )
        return [line.strip() for line in output.splitlines() if line.strip()]

    def list_pane_infos(self, window_name: str) -> list[PaneInfo]:
        """Return pane metadata including Slack Bridge ownership markers."""
        output = self._run(
            [
                "list-panes",
                "-t",
                self._window_target(window_name),
                "-F",
                "\t".join(
                    [
                        "#{pane_id}",
                        "#{pane_current_command}",
                        "#{pane_title}",
                        "#{pane_active}",
                        "#{pane_dead}",
                        "#{pane_start_command}",
                        f"#{{{BRIDGE_PANE_OWNED_OPTION}}}",
                        f"#{{{BRIDGE_PANE_INSTANCE_OPTION}}}",
                        f"#{{{BRIDGE_PANE_IDENTITY_OPTION}}}",
                    ]
                ),
            ],
        )
        panes: list[PaneInfo] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 9:
                parts.extend([""] * (9 - len(parts)))
            pane_id = parts[0].strip()
            if not pane_id:
                continue
            panes.append(PaneInfo(*parts[:9]))
        return panes

    def pane_exists(self, pane_id: str) -> bool:
        """ペインが存在するか確認"""
        try:
            self._run(["display-message", "-t", pane_id, "-p", ""], check=True)
            return True
        except RuntimeError as exc:
            message = str(exc).lower()
            if "can't find pane" in message or "no server running" in message:
                return False
            raise TmuxQueryError(f"tmux pane query failed for {pane_id}") from exc

    def pane_is_shell_or_dead(self, pane_id: str) -> bool:
        """Return True when a pane is gone/dead or has returned to an interactive shell."""
        try:
            output = self._run(
                [
                    "display-message",
                    "-t",
                    pane_id,
                    "-p",
                    "#{pane_current_command}\t#{pane_dead}",
                ],
                check=True,
            )
        except RuntimeError as exc:
            raise TmuxQueryError(f"tmux pane state query failed for {pane_id}") from exc
        command, _, dead = output.partition("\t")
        return dead.strip() == "1" or command.strip() in REUSABLE_SHELL_COMMANDS

    def pane_in_mode(self, pane_id: str) -> bool:
        """ペインがtmuxのコピー/選択モード中か確認"""
        try:
            output = self._run(
                ["display-message", "-t", pane_id, "-p", "#{pane_in_mode}"], check=True
            )
        except RuntimeError as exc:
            raise TmuxQueryError(f"tmux pane mode query failed for {pane_id}") from exc
        return output.strip() == "1"

    # --- ターゲット解決 ---

    def resolve_target(self, window_name: str, pane_id: str | None = None) -> str:
        """送信先のtmuxターゲットを解決"""
        if not pane_id:
            raise ValueError(f"pane_id is required for tmux target resolution: {window_name}")
        return pane_id

    # --- 入力送信 ---

    def send_keys(self, target: str, keys: str, literal: bool = True):
        """tmux send-keys でキーを送信"""
        args = ["send-keys", "-t", target]
        if literal:
            args.append("-l")
        args.append(keys)
        self._run(args)

    def interrupt(self, pane_id: str):
        """実行中の pane に SIGINT を送る"""
        try:
            self.send_keys(pane_id, "C-c", literal=False)
        except Exception as e:
            logger.debug("interrupt failed for %s: %s", pane_id, e)

    def send_enter(self, target: str):
        """Enterキーを送信"""
        self._run(["send-keys", "-t", target, "Enter"])

    def send_text_and_enter(self, target: str, text: str):
        """テキストを送信してEnter"""
        buffer_name = f"cc-send-{uuid.uuid4().hex}"
        temp_path = None
        try:
            if self.pane_in_mode(target):
                logger.debug("pane %s is in copy-mode; cancelling before paste", target)
            # Clear tmux modes so pasted input reaches the application reliably.
            self._run(["copy-mode", "-q", "-t", target], check=False)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".txt", delete=False
            ) as handle:
                handle.write(text)
                temp_path = handle.name
            self._run(["load-buffer", "-b", buffer_name, temp_path], timeout=TMUX_SEND_TIMEOUT)
            self._run(
                ["paste-buffer", "-d", "-p", "-r", "-b", buffer_name, "-t", target],
                timeout=TMUX_SEND_TIMEOUT,
            )
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
            try:
                self._run(["delete-buffer", "-b", buffer_name], check=False)
            except Exception:
                logger.debug("delete_buffer cleanup failed for %s", buffer_name)
        time.sleep(TMUX_PASTE_SETTLE_DELAY)
        self.send_enter(target)

    # --- 出力キャプチャ ---

    def capture_pane(self, target: str, lines: int = 50) -> str:
        """ペインの出力をキャプチャ"""
        return self._run(
            [
                "capture-pane",
                "-t",
                target,
                "-p",
                "-S",
                f"-{lines}",
            ]
        )
