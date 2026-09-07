"""Stdlib-only renderer for Slack Bridge fixed copy."""

from __future__ import annotations

import ast
import re
import string
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypeGuard

DEFAULT_MESSAGES_PATH = Path(__file__).resolve().parents[1] / "config" / "slack_messages.yaml"

_FORMATTER = string.Formatter()
_SINGLE_PLACEHOLDER_RE = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_LEAKED_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_PLACEHOLDER_CONTEXT_ALIASES = {"session_status": "status"}
_MESSAGE_ALIASES = {
    "default_ack": "ack",
    "input_wait_default": "input_waiting",
    "input_wait_free_input": "free_input",
    "input_wait_tool_permission_number": "tool_permission_numbered",
    "input_wait_ask_user_question": "input_wait_ask",
    "input_wait_plan_approval": "plan_approval",
    "session_capacity_failure": "session_limit",
    "tmux_pane_capacity_failure": "tmux_pane_limit",
}


class SlackCopyError(ValueError):
    """Raised when a Slack copy template cannot be rendered safely."""


class MissingPlaceholderError(SlackCopyError):
    """Raised when a required render context value is absent."""

    def __init__(self, message_key: str, placeholder: str) -> None:
        super().__init__(f"Missing placeholder '{placeholder}' for Slack message '{message_key}'.")
        self.message_key = message_key
        self.placeholder = placeholder


class LeakedPlaceholderError(SlackCopyError):
    """Raised when rendered Slack copy still contains a placeholder token."""

    def __init__(self, message_key: str, placeholder: str) -> None:
        super().__init__(
            f"Rendered Slack message '{message_key}' still contains placeholder '{placeholder}'."
        )
        self.message_key = message_key
        self.placeholder = placeholder


def load_messages(path: str | Path | None = None) -> dict:
    """Load Slack fixed-copy templates from the configured YAML file.

    This intentionally supports only the small YAML subset used by
    config/slack_messages.yaml so production does not need PyYAML.
    """

    source = Path(path) if path is not None else DEFAULT_MESSAGES_PATH
    data = _YamlSubsetParser(source.read_text(encoding="utf-8")).parse()
    if not isinstance(data, dict):
        raise SlackCopyError(f"Slack messages file must contain a mapping: {source}")
    return data


def render_message(
    key: str,
    context: Mapping[str, object] | None = None,
    messages: Mapping[str, object] | None = None,
) -> str:
    """Render one Slack fixed-copy template.

    Missing placeholders and placeholders leaked into final output are hard
    errors. Context values are expected to be Slack mrkdwn escaped by callers.
    """

    templates = messages if messages is not None else load_messages()
    template = _resolve_template(key, templates)
    if template is None:
        raise KeyError(f"Slack copy message not found: {key}")
    render_context = _normalize_context(context or {})
    output = _render_template(key, template, render_context)
    output = "\n".join(line.rstrip() for line in output.splitlines()).strip()
    _raise_for_leaked_placeholder(key, output)
    return output


def _resolve_template(key: str, templates: Mapping[str, object]) -> object | None:
    if key in templates:
        return templates[key]
    alias = _MESSAGE_ALIASES.get(key)
    if alias and alias in templates:
        return templates[alias]
    return None


def _normalize_context(context: Mapping[str, object]) -> dict[str, object]:
    normalized = dict(context)
    for placeholder, alias in _PLACEHOLDER_CONTEXT_ALIASES.items():
        if placeholder not in normalized and alias in normalized:
            normalized[placeholder] = normalized[alias]
    if "limit" not in normalized and "capacity" in normalized:
        normalized["limit"] = normalized["capacity"]
    if "capacity" not in normalized and "limit" in normalized:
        normalized["capacity"] = normalized["limit"]
    return normalized


class _YamlSubsetParser:
    def __init__(self, text: str) -> None:
        self._lines: list[tuple[int, str, int]] = []
        for lineno, raw_line in enumerate(text.splitlines(), start=1):
            if "\t" in raw_line:
                raise SlackCopyError(f"Tabs are not supported in YAML line {lineno}.")
            if not raw_line.strip() or raw_line.lstrip().startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            content = self._strip_comment(raw_line[indent:]).rstrip()
            if content:
                self._lines.append((indent, content, lineno))

    def parse(self) -> dict[str, Any]:
        if not self._lines:
            return {}
        data, index = self._parse_block(0, self._lines[0][0])
        if index != len(self._lines):
            _, _, lineno = self._lines[index]
            raise SlackCopyError(f"Unexpected YAML content at line {lineno}.")
        if not isinstance(data, dict):
            raise SlackCopyError("Slack messages YAML must start with a mapping.")
        return data

    def _parse_block(self, index: int, indent: int) -> tuple[Any, int]:
        if index >= len(self._lines):
            return {}, index
        line_indent, content, lineno = self._lines[index]
        if line_indent != indent:
            raise SlackCopyError(f"Unexpected indentation at YAML line {lineno}.")
        if content.startswith("- "):
            return self._parse_list(index, indent)
        return self._parse_mapping(index, indent)

    def _parse_mapping(self, index: int, indent: int) -> tuple[dict[str, Any], int]:
        result: dict[str, Any] = {}
        while index < len(self._lines):
            line_indent, content, lineno = self._lines[index]
            if line_indent < indent:
                break
            if line_indent > indent:
                raise SlackCopyError(f"Unexpected indentation at YAML line {lineno}.")
            if content.startswith("- "):
                raise SlackCopyError(f"Unexpected list item at YAML line {lineno}.")
            key, raw_value = self._split_key_value(content, lineno)
            if key in result:
                raise SlackCopyError(f"Duplicate YAML key '{key}' at line {lineno}.")
            if raw_value == "":
                next_index = index + 1
                if next_index < len(self._lines) and self._lines[next_index][0] > indent:
                    value, index = self._parse_block(next_index, self._lines[next_index][0])
                else:
                    value = {}
                    index = next_index
            else:
                value = self._parse_scalar(raw_value, lineno)
                index += 1
            result[key] = value
        return result, index

    def _parse_list(self, index: int, indent: int) -> tuple[list[Any], int]:
        result: list[Any] = []
        while index < len(self._lines):
            line_indent, content, lineno = self._lines[index]
            if line_indent < indent:
                break
            if line_indent > indent:
                raise SlackCopyError(f"Unexpected indentation at YAML line {lineno}.")
            if not content.startswith("- "):
                break
            raw_value = content[2:].strip()
            if raw_value == "":
                next_index = index + 1
                if next_index < len(self._lines) and self._lines[next_index][0] > indent:
                    value, index = self._parse_block(next_index, self._lines[next_index][0])
                else:
                    value = ""
                    index = next_index
            elif self._looks_like_inline_mapping(raw_value):
                key, value_text = self._split_key_value(raw_value, lineno)
                value = {key: self._parse_scalar(value_text, lineno) if value_text else {}}
                index += 1
            else:
                value = self._parse_scalar(raw_value, lineno)
                index += 1
            result.append(value)
        return result, index

    @staticmethod
    def _strip_comment(text: str) -> str:
        quote: str | None = None
        escaped = False
        for index, char in enumerate(text):
            if quote is not None:
                if quote == '"' and char == "\\" and not escaped:
                    escaped = True
                    continue
                if char == quote and not escaped:
                    quote = None
                escaped = False
                continue
            if char in {"'", '"'}:
                quote = char
                continue
            if char == "#" and (index == 0 or text[index - 1].isspace()):
                return text[:index].rstrip()
        return text

    @staticmethod
    def _split_key_value(content: str, lineno: int) -> tuple[str, str]:
        quote: str | None = None
        escaped = False
        for index, char in enumerate(content):
            if quote is not None:
                if quote == '"' and char == "\\" and not escaped:
                    escaped = True
                    continue
                if char == quote and not escaped:
                    quote = None
                escaped = False
                continue
            if char in {"'", '"'}:
                quote = char
                continue
            if char == ":":
                key = content[:index].strip()
                if not key:
                    raise SlackCopyError(f"Missing YAML key at line {lineno}.")
                return key, content[index + 1 :].strip()
        raise SlackCopyError(f"Expected 'key: value' at YAML line {lineno}.")

    @classmethod
    def _looks_like_inline_mapping(cls, value: str) -> bool:
        if value.startswith(('"', "'")):
            return False
        try:
            cls._split_key_value(value, 0)
        except SlackCopyError:
            return False
        return True

    @staticmethod
    def _parse_scalar(value: str, lineno: int) -> Any:
        if value in {"|", ">"}:
            raise SlackCopyError(f"Block scalars are not supported at YAML line {lineno}.")
        if value == "[]":
            return []
        if value == "{}":
            return {}
        lowered = value.lower()
        if lowered in {"null", "~"}:
            return None
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if re.fullmatch(r"-?\d+", value):
            return int(value)
        if value.startswith(('"', "'")):
            try:
                return ast.literal_eval(value)
            except (SyntaxError, ValueError) as exc:
                raise SlackCopyError(f"Invalid quoted scalar at YAML line {lineno}.") from exc
        return value


class _StrictFormatMap(dict[str, object]):
    def __init__(self, message_key: str, context: Mapping[str, object]) -> None:
        super().__init__(context)
        self._message_key = message_key

    def __missing__(self, key: str) -> object:
        raise MissingPlaceholderError(self._message_key, key)


def _render_template(message_key: str, template: object, context: Mapping[str, object]) -> str:
    if isinstance(template, str):
        return _format_text(message_key, template, context)
    if not isinstance(template, Mapping):
        raise SlackCopyError(f"Slack message '{message_key}' must be a string or mapping.")

    if "text" in template and not any(
        name in template for name in ("title", "quote", "body", "sections")
    ):
        return "\n".join(_format_lines(message_key, template["text"], context))

    lines: list[str] = []
    title = _format_text(message_key, str(template.get("title", "")), context).strip()
    if title:
        lines.append(f"*{title}*")

    quote_lines = _format_lines(message_key, template.get("quote"), context)
    lines.extend(f"> {line}" for line in quote_lines)

    body_lines = _format_lines(message_key, template.get("body"), context)
    if body_lines:
        if lines:
            lines.append("")
        if str(template.get("body_mode", "")).strip() == "quote":
            lines.extend(f"> {line}" for line in body_lines)
        else:
            lines.extend(body_lines)

    sections = template.get("sections") or {}
    if not isinstance(sections, Mapping):
        raise SlackCopyError(f"Slack message '{message_key}' sections must be a mapping.")
    for section_title, section_template in sections.items():
        rendered_section = _render_section(
            message_key, str(section_title), section_template, context
        )
        if not rendered_section:
            continue
        if lines:
            lines.append("")
        lines.extend(rendered_section)

    return "\n".join(lines)


def _render_section(
    message_key: str,
    section_title: str,
    section_template: object,
    context: Mapping[str, object],
) -> list[str]:
    if isinstance(section_template, str):
        content_lines = _format_lines(message_key, section_template, context)
        mode = "quote"
    elif isinstance(section_template, Mapping):
        mode = str(section_template.get("mode", "")).strip() or (
            "bullets" if "items" in section_template else "quote"
        )
        source = (
            section_template.get("items") if mode == "bullets" else section_template.get("text")
        )
        if source is None and mode != "bullets":
            source = section_template.get("items")
        content_lines = _format_lines(message_key, source, context)
    else:
        raise SlackCopyError(f"Slack message '{message_key}' has an invalid section.")

    if not content_lines:
        return []

    heading = _format_text(message_key, section_title, context).strip()
    lines = [f"*{heading}*"] if heading else []
    if mode == "bullets":
        lines.extend(
            line if line.lstrip().startswith("• ") else f"• {line}" for line in content_lines
        )
    elif mode == "quote":
        lines.extend(f"> {line}" for line in content_lines)
    elif mode == "text":
        lines.extend(content_lines)
    else:
        raise SlackCopyError(f"Unsupported section mode '{mode}' in Slack message '{message_key}'.")
    return lines


def _format_lines(
    message_key: str,
    value: object,
    context: Mapping[str, object],
) -> list[str]:
    if value is None:
        return []
    values = value if _is_non_string_sequence(value) else [value]
    lines: list[str] = []
    for item in values:
        expanded = _format_value(message_key, item, context)
        expanded_items = expanded if _is_non_string_sequence(expanded) else [expanded]
        for expanded_item in expanded_items:
            if expanded_item is None:
                continue
            for line in str(expanded_item).splitlines():
                line = line.rstrip()
                if line.strip():
                    lines.append(line)
    return lines


def _format_value(message_key: str, value: object, context: Mapping[str, object]) -> object:
    if not isinstance(value, str):
        return value
    match = _SINGLE_PLACEHOLDER_RE.fullmatch(value)
    if match:
        placeholder = match.group(1)
        if placeholder not in context:
            raise MissingPlaceholderError(message_key, placeholder)
        return context[placeholder]
    return _format_text(message_key, value, context)


def _format_text(message_key: str, template: str, context: Mapping[str, object]) -> str:
    for _, field_name, _, _ in _FORMATTER.parse(template):
        if not field_name:
            continue
        root_name = re.split(r"[.[]", field_name, maxsplit=1)[0]
        if root_name not in context:
            raise MissingPlaceholderError(message_key, root_name)
    try:
        return template.format_map(_StrictFormatMap(message_key, context))
    except MissingPlaceholderError:
        raise
    except Exception as exc:
        raise SlackCopyError(f"Failed to render Slack message '{message_key}': {exc}") from exc


def _raise_for_leaked_placeholder(message_key: str, output: str) -> None:
    match = _LEAKED_PLACEHOLDER_RE.search(output)
    if match:
        raise LeakedPlaceholderError(message_key, match.group(0))


def _is_non_string_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
