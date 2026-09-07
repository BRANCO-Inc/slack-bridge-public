"""Bridge extension registry facade."""

from __future__ import annotations

from extensions.base import BridgeExtension, PoolSpec
from extensions.core import CORE_EXTENSIONS
from extensions.registry import EXTENSIONS, load_extensions

__all__ = [
    "BridgeExtension",
    "CORE_EXTENSIONS",
    "EXTENSIONS",
    "PoolSpec",
    "load_extensions",
]
