"""Extension registry and private feature adapters.

The default profile keeps the current private behavior. Public profiles
load only core extensions so a distribution build can omit private modules.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import ModuleType
from typing import Any

from extensions.base import BridgeExtension
from extensions.core import CORE_EXTENSIONS
from runtime_env import (
    active_profile,
    private_profile_enabled,
)

OPTIONAL_EXTENSION_POOL_WAITING_REASON_PREFIX = "optional_extension_pool_waiting"
__all__ = [
    "EXTENSIONS",
    "OPTIONAL_EXTENSION_POOL_WAITING_REASON_PREFIX",
    "active_profile",
    "optional_extension_ack_text",
    "optional_extension_case_id_from_metadata",
    "optional_extension_notification_actor",
    "is_duplicate_optional_extension_notification",
    "is_optional_extension_analysis_report_reply",
    "is_optional_extension_notification_message",
    "is_optional_extension_notification_metadata",
    "is_optional_extension_pool_mismatch",
    "load_extensions",
    "mark_duplicate_optional_extension_notification",
    "private_extensions_enabled",
    "record_optional_extension_analysis_report_post_failure",
]


def private_extensions_enabled(profile: str | None = None) -> bool:
    return private_profile_enabled(profile)


def _load_optional_extension_module(profile: str | None = None) -> ModuleType | None:
    return None


def _load_private_extensions(profile: str | None = None) -> tuple[BridgeExtension, ...]:
    if not private_extensions_enabled(profile):
        return ()
    return ()


def load_extensions(profile: str | None = None) -> list[BridgeExtension]:
    private_enabled = private_extensions_enabled(profile)
    private_extensions = _load_private_extensions(profile) if private_enabled else ()
    return [*private_extensions, *CORE_EXTENSIONS]


EXTENSIONS: list[BridgeExtension] = load_extensions()


def optional_extension_ack_text(envelope: Mapping[str, Any]) -> str | None:
    module = _load_optional_extension_module()
    return module.optional_extension_ack_text(envelope) if module is not None else None


def is_optional_extension_notification_message(event: Mapping[str, Any]) -> bool:
    module = _load_optional_extension_module()
    return bool(module and module.is_optional_extension_notification_message(event))


def is_optional_extension_notification_metadata(metadata: Any) -> bool:
    module = _load_optional_extension_module()
    return bool(module and module.is_optional_extension_notification_metadata(metadata))


def is_optional_extension_pool_mismatch(session: Any, envelope: Mapping[str, Any]) -> bool:
    module = _load_optional_extension_module()
    return bool(module and module.is_optional_extension_pool_mismatch(session, envelope))


def is_duplicate_optional_extension_notification(session: Any, envelope: Mapping[str, Any]) -> bool:
    module = _load_optional_extension_module()
    return bool(module and module.is_duplicate_optional_extension_notification(session, envelope))


def mark_duplicate_optional_extension_notification(
    runtime: Any, session: Any, envelope: Mapping[str, Any]
) -> None:
    module = _load_optional_extension_module()
    if module is not None:
        module.mark_duplicate_optional_extension_notification(runtime, session, envelope)


def optional_extension_case_id_from_metadata(metadata: Mapping[str, Any]) -> str | None:
    module = _load_optional_extension_module()
    return module.optional_extension_case_id_from_metadata(metadata) if module is not None else None


def optional_extension_notification_actor(actor: dict) -> dict:
    module = _load_optional_extension_module()
    return module.optional_extension_notification_actor(actor) if module is not None else actor


def is_optional_extension_analysis_report_reply(text: str) -> bool:
    module = _load_optional_extension_module()
    return bool(module and module.is_optional_extension_analysis_report_reply(text))


def record_optional_extension_analysis_report_post_failure(**kwargs: Any) -> dict:
    module = _load_optional_extension_module()
    if module is None:
        return {"ok": True, "skipped": True, "reason": "optional_extension_feature_disabled"}
    return module.record_optional_extension_analysis_report_post_failure(**kwargs)
