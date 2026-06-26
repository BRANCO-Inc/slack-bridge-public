"""入力待ち検出モジュール"""

import hashlib
import re
import threading
import time
from datetime import UTC, datetime
from typing import NotRequired, TypedDict

from bridge_logging import get_logger
from config import (
    FREE_INPUT_GRACE_PERIOD,
    INPUT_DETECT_INTERVAL,
    INPUT_DETECT_STABLE_THRESHOLD,
)
from session import Session, Status
from slack_copy.renderer import render_message

logger = get_logger(__name__)

ASK_USER_QUESTION_META_OPTIONS = {
    "Type something.",
    "Chat about this",
}
MENU_META_OPTION_PREFIXES = ("Type here",)

ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
SPINNER_GLYPHS_RE = re.compile(r"^[\s┃│|/\\\-*✶✽✻✢·•●○◐◓◑◒⠁-⣿]+(?=\S)")

PERMISSION_DIALOG_HEADERS = (
    "bash command",
    "edit file",
    "write file",
)
PERMISSION_DIALOG_OPTION_RE = re.compile(
    r"(?:^|\n)\s*(?:[❯>]\s*)?(?:\[\s*)?([123])(?:\s*\]|\.)\s+"
    r"(yes(?:,|\b)|no(?:,|\b))",
    re.IGNORECASE,
)


class InputWaitPattern(TypedDict):
    """INPUT_WAIT_PATTERNS のエントリ構造"""

    type: str
    description: str
    pattern: str
    response_format: str
    requires_stable: NotRequired[bool]
    stable_count: NotRequired[int]
    last_line_only: NotRequired[bool]


class MenuOptionEntry(TypedDict):
    """_extract_menu_option_entries が返すメニュー選択肢の構造"""

    number: str
    label: str
    text: str
    meta: bool


# 入力待ちパターン定義
INPUT_WAIT_PATTERNS: list[InputWaitPattern] = [
    {
        "type": "ask_user_question",
        "description": "AskUserQuestionの選択肢表示",
        # [1] option1
        # [2] option2
        # [3] Other
        "pattern": r"\[\d+\]\s+.+\n.*\[\d+\]\s+.+",
        "response_format": "number",  # 番号で回答
    },
    {
        "type": "free_input",
        "description": "自由入力待ち（❯プロンプトが一定時間変化なし）",
        "pattern": r"❯\s*$",
        "response_format": "text",
        "requires_stable": True,  # 安定検出が必要（一度だけでは確定しない）
        "stable_count": 10,  # 3秒×10回 = 30秒間変化なし
        "last_line_only": True,  # 最終行のみでマッチ（出力途中の❯を誤検出しない）
    },
]

CONTROL_COMMAND_PATTERNS = {
    "cancel": re.compile(r"^\s*キャンセル\s*$"),
    "end": re.compile(r"^\s*終了\s*$"),
}


def detect_session_control_command(text: str) -> str | None:
    """Slack 文言からセッション制御コマンドを抽出する。"""
    if not text:
        return None
    for command, pattern in CONTROL_COMMAND_PATTERNS.items():
        if pattern.fullmatch(text):
            return command
    return None


class InputWaitResult:
    """入力待ち検出結果"""

    def __init__(
        self,
        wait_type: str,
        description: str,
        response_format: str,
        raw_output: str,
        options: list[str] | None = None,
        free_text_option: str | None = None,
        summary_lines: list[str] | None = None,
    ):
        self.wait_type = wait_type
        self.description = description
        self.response_format = response_format  # "number" | "number_or_text" | "text"
        self.raw_output = raw_output
        self.options = options or []
        self.free_text_option = free_text_option
        self.summary_lines = summary_lines or []

    def format_for_slack(self) -> str:
        """Slack通知用のメッセージを生成"""
        if self.wait_type == "ask_user_question":
            return render_message(
                "input_wait_ask",
                {"options": self.options},
            )
        elif self.wait_type == "plan_approval":
            return render_message(
                "plan_approval",
                {"summary_lines": self.summary_lines},
            )
        elif self.wait_type == "tool_permission":
            return render_message("tool_permission_numbered")
        elif self.wait_type == "free_input":
            return render_message("free_input")
        return render_message(
            "input_waiting",
            {"description": self.description.strip() if self.description else "未記載"},
        )


class InputDetector:
    """busyセッションの入力待ちを検出するポーリングデーモン"""

    def __init__(self, tmux_manager, session_store, on_detected_callback):
        """
        Args:
            tmux_manager: TmuxManager instance
            session_store: SessionStore instance
            on_detected_callback: func(session, InputWaitResult) → called when input wait detected
        """
        self.tmux = tmux_manager
        self.store = session_store
        self.on_detected = on_detected_callback
        self._thread = None
        self._stop_event = threading.Event()
        # パターンごとの連続検出カウンタ {session_key: {pattern_type: count}}
        self._stable_counts: dict[str, dict[str, int]] = {}
        # 前回のキャプチャ内容 {session_key: str}
        self._last_captures: dict[str, str] = {}
        # 直近のturn marker {session_key: marker}
        self._turn_markers: dict[str, str] = {}
        # 直近の検出エラー {session_key: message}
        self._last_errors: dict[str, str] = {}
        # 通知済みprompt {session_key: {fingerprint: metadata}}
        self._notified_prompts: dict[str, dict[str, dict[str, str | float | None]]] = {}

    def start(self):
        """ポーリングスレッド開始"""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.info("Started input detector (interval=%ss)", INPUT_DETECT_INTERVAL)

    def stop(self):
        """ポーリング停止"""
        self._stop_event.set()

    def clear_session(self, thread_ts: str):
        """セッションの検出カウンタをリセット（状態遷移時に呼ぶ）"""
        keys = {
            key
            for key in (
                set(self._stable_counts)
                | set(self._last_captures)
                | set(self._turn_markers)
                | set(self._last_errors)
                | set(self._notified_prompts)
            )
            if key == thread_ts or key.endswith(f":{thread_ts}")
        }
        keys.add(thread_ts)
        for key in keys:
            self._stable_counts.pop(key, None)
            self._last_captures.pop(key, None)
            self._turn_markers.pop(key, None)
            self._last_errors.pop(key, None)
            self._notified_prompts.pop(key, None)

    def _poll_loop(self):
        while not self._stop_event.is_set():
            try:
                self._check_all_busy_sessions()
            except Exception as e:
                logger.exception("Error in poll loop: %s", e)
            self._stop_event.wait(INPUT_DETECT_INTERVAL)

    def _check_all_busy_sessions(self):
        for session in self.store.list_all():
            if session.status is not Status.BUSY:
                continue
            pane_id = session.pane_id
            if not pane_id:
                continue
            try:
                output = self.tmux.capture_pane(pane_id)
                self._clear_error(session)
                result = self._match_patterns(session, output)
                if result:
                    self.on_detected(session, result)
            except RuntimeError as e:
                self._record_error(session, f"capture skipped: {e}")
            except Exception as e:
                self._record_error(session, f"input detection failed: {e}")
                logger.exception("input detection failed for %s: %s", session.thread_ts, e)

    def _match_patterns(self, session: Session, output: str) -> InputWaitResult | None:
        session_key = self._session_key(session)
        self._reset_turn_state_if_needed(session, session_key)
        normalized_output = self._normalize_output_for_detection(output)
        had_stable_pending = False
        menu_entries = self._extract_menu_option_entries(normalized_output)
        menu_options = [entry["text"] for entry in menu_entries if not entry["meta"]]
        free_text_option = self._extract_free_text_option(menu_entries)
        plan_option_entries = self._extract_plan_approval_option_entries(normalized_output)
        plan_options = [entry["text"] for entry in plan_option_entries]

        if self._looks_like_permission_dialog(normalized_output):
            return self._dedupe_result(
                session,
                session_key,
                InputWaitResult(
                    wait_type="tool_permission",
                    description="Codex permission dialog",
                    response_format="number",
                    raw_output=output[-500:],
                    options=menu_options,
                ),
                normalized_output,
            )

        if self._looks_like_plan_approval_menu(normalized_output, plan_options):
            return self._dedupe_result(
                session,
                session_key,
                InputWaitResult(
                    wait_type="plan_approval",
                    description="Plan Mode実行承認要求",
                    response_format="number_or_text",
                    raw_output=output[-500:],
                    options=plan_options,
                    summary_lines=self._extract_plan_summary_lines(normalized_output),
                ),
                normalized_output,
            )

        for pattern_def in INPUT_WAIT_PATTERNS:
            ptype = pattern_def["type"]
            if ptype == "ask_user_question":
                if self._looks_like_ask_user_question(normalized_output, menu_options):
                    return self._dedupe_result(
                        session,
                        session_key,
                        InputWaitResult(
                            wait_type=ptype,
                            description=pattern_def["description"],
                            response_format=pattern_def["response_format"],
                            raw_output=output[-500:],
                            options=menu_options,
                            free_text_option=free_text_option,
                        ),
                        normalized_output,
                    )
                continue

            # last_line_only: 最終行のみでマッチ（free_inputの誤検出防止）
            match_target = normalized_output
            if pattern_def.get("last_line_only"):
                match_target = self._last_nonempty_line(normalized_output)
            if re.search(pattern_def["pattern"], match_target, re.MULTILINE | re.IGNORECASE):
                if ptype == "free_input" and self._is_in_free_input_grace_period(session):
                    continue

                # 安定検出が必要なパターン
                if pattern_def.get("requires_stable"):
                    counts = self._stable_counts.setdefault(session_key, {})
                    last = self._last_captures.get(session_key, "")

                    if normalized_output == last:
                        counts[ptype] = counts.get(ptype, 0) + 1
                    else:
                        counts[ptype] = 1
                    self._last_captures[session_key] = normalized_output

                    required = pattern_def.get("stable_count", INPUT_DETECT_STABLE_THRESHOLD)
                    if counts[ptype] < required:
                        had_stable_pending = True
                        continue  # まだ安定していない

                # 即時検出 or 安定条件クリア（ask_user_question はループ前半で処理済み）
                options: list[str] = []

                return self._dedupe_result(
                    session,
                    session_key,
                    InputWaitResult(
                        wait_type=ptype,
                        description=pattern_def["description"],
                        response_format=pattern_def["response_format"],
                        raw_output=output[-500:],  # 直近500文字
                        options=options,
                    ),
                    normalized_output,
                )

        # 安定待ちパターンがあった場合はカウンタを保持（リセットしない）
        if not had_stable_pending:
            # どのパターンにもマッチしなかった → カウンタリセット
            self._stable_counts.pop(session_key, None)
        self._last_captures[session_key] = normalized_output
        return None

    def _normalize_output_for_detection(self, output: str) -> str:
        output = ANSI_ESCAPE_RE.sub("", output)
        output = output.replace("\r", "\n").replace("\u00a0", " ")
        normalized_lines = []
        for line in output.splitlines():
            stripped_right = line.rstrip()
            stripped_left = stripped_right.lstrip()
            if re.fullmatch(r"[\s┃│|/\\\-*✶✽✻✢·•●○◐◓◑◒⠁-⣿]+", stripped_right):
                continue
            if re.match(r"^[┃│|/\\\-*✶✽✻✢·•●○◐◓◑◒⠁-⣿]\s+", stripped_left):
                indent_len = len(stripped_right) - len(stripped_left)
                stripped_right = (" " * indent_len) + SPINNER_GLYPHS_RE.sub("", stripped_left)
            normalized_lines.append(stripped_right)
        return "\n".join(normalized_lines)

    def _looks_like_permission_dialog(self, output: str) -> bool:
        lower = output.lower()
        if "do you want to proceed?" not in lower:
            return False
        if not any(header in lower for header in PERMISSION_DIALOG_HEADERS):
            return False
        choices = {match.group(1) for match in PERMISSION_DIALOG_OPTION_RE.finditer(output)}
        return {"1", "2", "3"}.issubset(choices)

    def _dedupe_result(
        self,
        session: Session,
        session_key: str,
        result: InputWaitResult,
        normalized_output: str,
    ) -> InputWaitResult | None:
        fingerprint = self._prompt_fingerprint(session, result, normalized_output)
        notified = self._notified_prompts.setdefault(session_key, {})
        if fingerprint in notified:
            return None
        notified[fingerprint] = {
            "prompt_hash": fingerprint,
            "turn_id": session.active_turn_id,
            "pane_id": session.pane_id,
            "notified_at": time.time(),
        }
        return result

    def _prompt_fingerprint(
        self,
        session: Session,
        result: InputWaitResult,
        normalized_output: str,
    ) -> str:
        prompt_tail = normalized_output[-2000:]
        options = "\n".join(result.options)
        digest = hashlib.sha256(
            f"{result.wait_type}\n{options}\n{prompt_tail}".encode()
        ).hexdigest()
        return f"{self._turn_marker(session)}:{session.pane_id or ''}:{digest}"

    def _is_in_free_input_grace_period(self, session: Session) -> bool:
        reference = session.active_turn_started_at or session.created_at
        if not reference:
            return False
        try:
            created_dt = datetime.fromisoformat(reference)
        except ValueError:
            return False
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=UTC)
        return (time.time() - created_dt.timestamp()) < FREE_INPUT_GRACE_PERIOD

    def _looks_like_ask_user_question(self, output: str, options: list[str]) -> bool:
        if len(options) < 2:
            return False
        if self._looks_like_plan_approval_menu(output, options):
            return False
        if "Enter to select" in output or "Type something." in output:
            return True

        lines = [line.strip() for line in output.splitlines() if line.strip()]
        option_indexes = [
            index
            for index, line in enumerate(lines)
            if re.match(r"^(?:\[\d+\]|\d+\.)\s+.+", re.sub(r"^❯\s*", "", line))
        ]
        if len(option_indexes) < 2:
            return False

        prefix = lines[: option_indexes[0]]
        question_tail = " ".join(prefix[-2:]).lower()
        return (
            "?" in question_tail
            or "option" in question_tail
            or "select" in question_tail
            or "choose" in question_tail
            or "question" in question_tail
        )

    def _looks_like_plan_approval_menu(self, output: str, options: list[str]) -> bool:
        if len(options) < 3:
            return False
        lower = output.lower()
        if "ready to code?" not in lower and "would you like to proceed?" not in lower:
            return False
        return any(option.startswith(("1. Yes", "[1] Yes")) for option in options)

    def _extract_plan_approval_option_entries(self, output: str) -> list[dict[str, str]]:
        lines = output.splitlines()
        start_index = 0
        for index, line in enumerate(lines):
            lower = line.lower()
            if "would you like to proceed?" in lower or "ready to code?" in lower:
                start_index = index
        return [
            {
                "number": str(entry["number"]),
                "label": str(entry["label"]),
                "text": str(entry["text"]),
            }
            for entry in self._extract_menu_option_entries("\n".join(lines[start_index:]))
            if not entry["meta"]
        ]

    def _extract_plan_summary_lines(self, output: str) -> list[str]:
        lines = output.splitlines()
        started = False
        summary: list[str] = []
        heading_map = {
            "Context": "*背景*",
            "Plan": "*プラン*",
            "やること": "*やること*",
            "対象ファイル": "*対象ファイル*",
            "検証方法": "*検証方法*",
        }

        for line in lines:
            stripped = line.strip()
            lower = stripped.lower()
            if (
                "here is codex's plan:" in lower
                or "here is claude's plan:" in lower
                or "here's claude's plan:" in lower
                or "here is my plan:" in lower
            ):
                started = True
                continue
            if not started:
                continue
            if "would you like to proceed?" in lower:
                break
            if not stripped or all(char in "─╌-" for char in stripped):
                continue
            if stripped.startswith("Plan:"):
                summary.append(f"*プラン* {stripped.split(':', 1)[1].strip()}")
                continue
            if stripped in heading_map:
                if summary and summary[-1] != "":
                    summary.append("")
                summary.append(heading_map[stripped])
                continue
            if re.match(r"^\d+\.\s+", stripped):
                item = re.sub(r"^\d+\.\s+", "", stripped)
                summary.append(f"• {item}")
                continue
            if stripped.startswith("- "):
                summary.append(f"• {stripped[2:].strip()}")
                continue
            summary.append(stripped)

        compact = [line for line in summary if line]
        return compact[:8]

    def _extract_menu_option_entries(self, output: str) -> list[MenuOptionEntry]:
        options: list[MenuOptionEntry] = []
        for line in output.splitlines():
            stripped = line.strip()
            bracket = re.match(r"^\[(\d+)\]\s+(.+)", stripped)
            if bracket:
                number, label = bracket.group(1), bracket.group(2).strip()
                options.append(
                    {
                        "number": number,
                        "label": label,
                        "text": stripped,
                        "meta": self._is_meta_option_label(label),
                    }
                )
                continue
            dotted = re.match(r"^(?:❯\s*)?(\d+)\.\s+(.+)", stripped)
            if dotted:
                number, label = dotted.group(1), dotted.group(2).strip()
                text = re.sub(r"^❯\s*", "", stripped)
                options.append(
                    {
                        "number": number,
                        "label": label,
                        "text": text,
                        "meta": self._is_meta_option_label(label),
                    }
                )
        return options

    def _extract_free_text_option(self, menu_entries: list[MenuOptionEntry]) -> str | None:
        preferred = (
            "Type something.",
            "Chat about this",
        )
        for label in preferred:
            for entry in menu_entries:
                if entry["label"] == label:
                    return str(entry["number"])
        return None

    def _is_meta_option_label(self, label: str) -> bool:
        return label in ASK_USER_QUESTION_META_OPTIONS or any(
            label.startswith(prefix) for prefix in MENU_META_OPTION_PREFIXES
        )

    def _session_key(self, session: Session) -> str:
        return session.conversation_identity or session.thread_ts

    def _turn_marker(self, session: Session) -> str:
        return session.active_turn_id or session.active_turn_started_at or "_no_turn_"

    def _reset_turn_state_if_needed(self, session: Session, session_key: str) -> None:
        marker = self._turn_marker(session)
        previous = self._turn_markers.get(session_key)
        if previous == marker:
            return
        self._stable_counts.pop(session_key, None)
        self._last_captures.pop(session_key, None)
        if marker:
            self._turn_markers[session_key] = marker
        else:
            self._turn_markers.pop(session_key, None)

    def _last_nonempty_line(self, output: str) -> str:
        lines = [line.rstrip() for line in output.splitlines() if line.strip()]
        return lines[-1] if lines else ""

    def _record_error(self, session: Session, message: str) -> None:
        session_key = self._session_key(session)
        if self._last_errors.get(session_key) == message:
            return
        self._last_errors[session_key] = message
        logger.warning("%s [%s]", message, session.thread_ts)

    def _clear_error(self, session: Session) -> None:
        self._last_errors.pop(self._session_key(session), None)
