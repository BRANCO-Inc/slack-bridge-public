"""Bridge 拡張の宣言的規格。

個別ユースケース（optional_extension / optional_extension / ai_boot など）の本体散在を防ぐための
最小規約。ローダーや動的登録は持たず、`extensions/__init__.py` の明示リスト
`EXTENSIONS` だけが登録点になる。

依存方向の規約:
- extensions/* が import してよいのは config / bridge_logging / slack_copy /
  標準ライブラリのみ。
- 本体（slack_bridge / hook_server / worker_process / message_dispatch /
  pane_pool / session*）→ extensions の一方向。逆依存は禁止。
- フックが受け取る runtime / session / dispatch は duck typing（Any）で扱う。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PoolSpec:
    """worker 実行環境（tmux pane pool）の宣言的定義。

    pool の窓集合・作業ディレクトリ・起動タイムアウト・失敗時文言と、
    pane 割当失敗時に ERROR ではなく SUSPENDED + 再キューへ逃がす判定を持つ。
    """

    pool_name: str
    windows: tuple[str, ...]
    workdir: str
    ready_timeout: int
    sandbox_profile: Callable[[], str] | None = None
    launch_error_text: str = ""
    ready_timeout_text: str = ""
    suspend_allocation_failure: Callable[[Any], bool] | None = None
    allocation_failure_reason_prefix: str = ""


@dataclass(frozen=True)
class TurnContext:
    """prepend_prompt フックへ渡す、既存で渡っている値の薄い束ね。"""

    session: Any = None
    reply_command_path: str = ""


@dataclass(frozen=True)
class OutboundContext:
    """transform_outbound フックへ渡す、既存で渡っている値の薄い束ね。"""

    channel_id: str
    thread_ts: str
    substantive: bool
    conversation_identity: str
    store: Any
    dispatch: Any


@dataclass(frozen=True)
class OutboundResult:
    """transform_outbound の結果。text を投稿本文として採用し、extra を post 結果へ合流する。"""

    text: str
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BridgeExtension:
    """1 ユースケース分のフック束。フックは全て Optional。

    - match_envelope: チャンネル / metadata / リアクション判定
    - pool: 実行環境ルーティング（使う拡張のみ）
    - handle_envelope: inbound fast-path。True を返すと消費（worker を起動しない）
    - prepend_prompt: worker プロンプトへの注入
    - transform_outbound: worker 返信の後処理（契約抽出など）。None なら変更なし
    """

    name: str
    match_envelope: Callable[[Any], bool] | None = None
    pool: PoolSpec | None = None
    handle_envelope: Callable[[Any, Any], bool] | None = None
    prepend_prompt: Callable[[str, Any, TurnContext], str] | None = None
    transform_outbound: Callable[[str, OutboundContext], OutboundResult | None] | None = None


def handle_envelope_extensions(
    extensions: Iterable[BridgeExtension], runtime: Any, envelope: Any
) -> bool:
    """inbound fast-path ループ。最初に True を返した拡張で消費し、以降は呼ばない。"""
    for extension in extensions:
        if extension.handle_envelope is None:
            continue
        if extension.handle_envelope(runtime, envelope):
            return True
    return False


def pool_name_for_envelope(extensions: Iterable[BridgeExtension], envelope: Any) -> str | None:
    """envelope にマッチした拡張の pool 名を返す。該当なしは None（=デフォルト pool）。"""
    for extension in extensions:
        if extension.pool is None or extension.match_envelope is None:
            continue
        if extension.match_envelope(envelope):
            return extension.pool.pool_name
    return None


def find_pool_spec(extensions: Iterable[BridgeExtension], pool_name: str) -> PoolSpec | None:
    for extension in extensions:
        if extension.pool is not None and extension.pool.pool_name == pool_name:
            return extension.pool
    return None


def apply_prompt_extensions(
    extensions: Iterable[BridgeExtension], payload: str, envelope: Any, turn_ctx: TurnContext
) -> str:
    """prepend_prompt フックをリスト順に適用する。"""
    for extension in extensions:
        if extension.prepend_prompt is None:
            continue
        payload = extension.prepend_prompt(payload, envelope, turn_ctx)
    return payload


def make_outbound_pipeline(
    base_post_to_slack: Callable[..., dict],
    extensions: Iterable[BridgeExtension],
    *,
    store: Any,
    dispatch: Any,
    conversation_identity_for: Callable[[str, str], str],
) -> Callable[..., dict]:
    """worker 返信の後処理パイプライン。

    各拡張の transform_outbound をリスト順に適用してから base post を呼び、
    OutboundResult.extra を post 結果 dict に合流させる。
    """

    def post_to_slack(
        channel_id: str,
        thread_ts: str,
        text: str,
        *,
        substantive: bool = False,
    ) -> dict:
        context = OutboundContext(
            channel_id=channel_id,
            thread_ts=thread_ts,
            substantive=substantive,
            conversation_identity=conversation_identity_for(channel_id, thread_ts),
            store=store,
            dispatch=dispatch,
        )
        extra: dict[str, Any] = {}
        for extension in extensions:
            if extension.transform_outbound is None:
                continue
            outcome = extension.transform_outbound(text, context)
            if outcome is None:
                continue
            text = outcome.text
            extra.update(outcome.extra)
        result = base_post_to_slack(channel_id, thread_ts, text, substantive=substantive)
        if extra and isinstance(result, dict):
            result.update(extra)
        return result

    return post_to_slack
