"""Bridge extension registry facade."""

from __future__ import annotations

from extensions.base import BridgeExtension, PoolSpec
from extensions.core import CORE_EXTENSIONS
from extensions.registry import (
    EXTENSIONS,
    active_profile,
    load_extensions,
    private_extensions_enabled,
)

__all__ = [
    "BridgeExtension",
    "CORE_EXTENSIONS",
    "EXTENSIONS",
    "PoolSpec",
    "active_profile",
    "load_extensions",
    "private_extensions_enabled",
]
