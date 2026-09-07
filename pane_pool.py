"""Pane pool naming and routing rules for Slack Bridge tmux windows."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import extensions
from config import (
    GENERAL_PANE_POOL_MAX_PANES,
    GENERAL_PANE_POOL_NAME,
    GENERAL_PANE_POOL_WINDOWS,
    LEGACY_TO_CURRENT_WINDOW_NAMES,
    PANE_POOL_WINDOW_MAX_PANES,
)
from extensions.base import pool_name_for_envelope as _extension_pool_name_for_envelope


@dataclass(frozen=True)
class PanePoolDefinition:
    pool_name: str
    window_names: tuple[str, ...]
    window_max_panes: int
    total_max_panes: int


@dataclass(frozen=True)
class PaneAllocation:
    pool_name: str
    window_name: str
    pane_id: str


PANE_POOLS: dict[str, PanePoolDefinition] = {
    GENERAL_PANE_POOL_NAME: PanePoolDefinition(
        pool_name=GENERAL_PANE_POOL_NAME,
        window_names=GENERAL_PANE_POOL_WINDOWS,
        window_max_panes=PANE_POOL_WINDOW_MAX_PANES,
        total_max_panes=GENERAL_PANE_POOL_MAX_PANES,
    ),
}

_WINDOW_TO_POOL = {
    window_name: definition.pool_name
    for definition in PANE_POOLS.values()
    for window_name in definition.window_names
}


def normalize_pool_name(pool_name: str | None) -> str:
    if not pool_name:
        return GENERAL_PANE_POOL_NAME
    if pool_name not in PANE_POOLS:
        raise ValueError(f"unknown pane pool: {pool_name}")
    return pool_name


def pool_definition(pool_name: str | None) -> PanePoolDefinition:
    return PANE_POOLS[normalize_pool_name(pool_name)]


def windows_for_pool(pool_name: str | None) -> list[str]:
    return list(pool_definition(pool_name).window_names)


def known_windows_for_pool(pool_name: str | None) -> list[str]:
    return windows_for_pool(pool_name)


def all_window_names() -> list[str]:
    return [
        window_name for definition in PANE_POOLS.values() for window_name in definition.window_names
    ]


def legacy_window_names() -> list[str]:
    return list(LEGACY_TO_CURRENT_WINDOW_NAMES)


def all_known_window_names() -> list[str]:
    return all_window_names()


def canonical_window_name(window_name: str | None) -> str | None:
    if not window_name:
        return window_name
    return LEGACY_TO_CURRENT_WINDOW_NAMES.get(window_name, window_name)


def pool_name_for_window(window_name: str | None) -> str:
    canonical_name = canonical_window_name(window_name)
    if canonical_name in _WINDOW_TO_POOL:
        return _WINDOW_TO_POOL[canonical_name]
    return GENERAL_PANE_POOL_NAME


def pool_definition_for_window(window_name: str | None) -> PanePoolDefinition:
    return pool_definition(pool_name_for_window(window_name))


def window_max_panes(window_name: str | None) -> int:
    return pool_definition_for_window(window_name).window_max_panes


def capacity_for_pool(pool_name: str | None) -> int:
    return pool_definition(pool_name).total_max_panes


def _mapping_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def pool_for_envelope(envelope: Mapping[str, Any] | None) -> str:
    pool_name = _extension_pool_name_for_envelope(extensions.EXTENSIONS, envelope)
    return pool_name or GENERAL_PANE_POOL_NAME


def pool_for_session(session: Any) -> str:
    window_name = session if isinstance(session, str) else _mapping_value(session, "window_name")
    return pool_name_for_window(window_name)
