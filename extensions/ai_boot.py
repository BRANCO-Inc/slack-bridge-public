"""ai-boot リアクション（スレッド文脈からの提案起動）ユースケースの Bridge 拡張。

指示文と判定の SSoT。envelope の構築（Slack API 呼び出し込み）は
envelope.build_ai_boot_reaction_envelope の責務のまま。
"""

from __future__ import annotations

from typing import Any

from extensions.base import BridgeExtension

AI_BOOT_INSTRUCTION = (
    "スレッド文脈を読み、勝手に実行せず、Slackには"
    "『これ私がやりましょうか？やるなら〇〇まで進めます』の提案だけ短く返す。"
    "必要な確認がある場合だけ1点聞く。"
)


def is_ai_boot_reaction_event(envelope: Any) -> bool:
    if not isinstance(envelope, dict):
        return False
    return envelope.get("normalized_event_type") == "ai_boot_reaction"


AI_BOOT = BridgeExtension(
    name="ai_boot",
    match_envelope=is_ai_boot_reaction_event,
)
