"""Public core extension registry entries."""

from __future__ import annotations

from extensions.ai_boot import AI_BOOT
from extensions.base import BridgeExtension

CORE_EXTENSIONS: tuple[BridgeExtension, ...] = (AI_BOOT,)
