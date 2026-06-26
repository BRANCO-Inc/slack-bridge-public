# Slack Bridge Public Smoke Test

## Overview

この手順は setup と doctor が通った後、Slack API への接続と worker 起動を最小限で確認するための手動チェックです。

## Before You Start

- `venv/bin/python scripts/doctor.py` が成功している
- Windows の場合は `.\venv\Scripts\python.exe scripts\doctor.py` が成功している
- Slack App manifest を Slack 側へ反映している
- Bot token、App token を `.env` に設定している

## Steps

1. tmux セッションを作る。

   ```bash
   tmux new-session -d -s slack-bridge -c "$PWD"
   ```

2. Slack Bridge を起動する。

   ```bash
   venv/bin/python scripts/run.py
   ```

   Windows の場合:

   ```powershell
   .\venv\Scripts\python.exe scripts\run.py
   ```

3. Slack の対象チャンネルで Bot にメンションする。

   ```text
   @Slack Bridge Assistant テストです。短く返信してください。
   ```

4. Slack スレッドに worker から返信が返ることを確認する。

5. 返信が返らない場合は doctor を再実行し、hook port、tmux、shell、選択中の AI worker CLI の順に確認する。

## Do Not Automate Here

- Slack token の値をログに出さない。
- Slack API への接続確認を doctor に混ぜない。
- 本番チャンネルで最初の動作確認をしない。
