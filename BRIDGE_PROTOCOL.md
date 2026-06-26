# Slack Bridge Protocol

## Use When

Slack Bridge worker がターンを完了し、Slack スレッドへ返信または進捗共有する時に使う通信契約。

## Reply Command Contract

- 各ターンで渡される `[reply_command: ...]` だけを使って Slack に返信する。
- 最終回答は `reply_command` の通常実行、または `reply_command --done` を使う。
- ユーザー待ちに入る時は `reply_command --wait` を使う。
- 進捗共有が必要な時だけ `reply_command --note` を使う。
- 進捗共有で `reply_command` を通常実行しない。通常実行は `done` 扱いになる。
- `case_reply.sh` を直接呼ばず、そのターンで渡された `reply_command` を使う。

## Turn Context

- 起動プロンプト内の Slack イベント文脈を優先する。
- `[turn_id: ...]` と `[reply_command: ...]` はターンごとの契約として扱う。
- `event_envelope_json` がある場合は、返信先、スレッド、依頼者、添付情報を確認する。
- セッション固有の情報を別スレッドへ持ち出さない。

## Safety

- 秘密情報、トークン、認証値を返信やログに出さない。
- `knowledge/personal/` は使わない。
- 実行可否が不明な外部副作用は、実行前に確認する。
