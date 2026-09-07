"""Public extension registry."""

from __future__ import annotations

from extensions.base import BridgeExtension
from extensions.core import CORE_EXTENSIONS

__all__ = ["EXTENSIONS", "load_extensions"]


def load_extensions() -> list[BridgeExtension]:
    return list(CORE_EXTENSIONS)


EXTENSIONS: list[BridgeExtension] = load_extensions()
