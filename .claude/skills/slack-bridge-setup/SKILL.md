---
name: slack-bridge-setup
description: Slack Bridgeの初回セットアップ、会社名・Bot名の設定、Mac/Linux/Windows WSL2での導入・起動診断を行う。初期設定して、Windowsで動かして、起動できない等の依頼で使う。
---

# Slack Bridge setup

このリポジトリの初回設定を実行する。作業場所はこのSkillディレクトリから3階層上のリポジトリルート。全コマンドはそのルートで実行し、最初に `docs/setup.md` を読む。Windowsの場合は `docs/windows.md` も読む。環境を準備したら `venv/bin/python scripts/configure.py --help` で引数を確認する。

## 完了までの手順

- [ ] OSと実行場所、既存設定の有無を確認する
- [ ] Python 3.14・tmux・AI CLI・venvを準備する
- [ ] 会社名・Bot名・利用するAIを確認して設定する
- [ ] 本人のSlack App作成・秘密トークン入力を案内する
- [ ] doctorを実行し、残るエラーを修正する
- [ ] 起動方法と動作確認、本人操作の残りを報告する

## 1. 環境を準備する

OS、シェル、現在のディレクトリ、`git status --short`、Python・tmux・選択したAI CLIの有無を調べる。`.env` やログの本文は表示しない。設定済みの場合は、その内容を壊さず必要な箇所だけ変更する。

WindowsのBridgeはWSL2で動かす。Windows側のPythonやCLI、`/mnt/c` 配下を実行環境にせず、WSLのLinuxファイルシステム内で通常ユーザーとして作業する。WSL未導入・再起動待ちはWindows手順の本人操作を案内し、再開場所を伝える。WSL内にAI CLIが無ければ公式手順で導入し、本人にログインしてもらう。Windows側の認証がWSLにもあると仮定しない。

macOS/Linuxでは不足する依存だけを既存のパッケージ管理方法と公式の導入手順で用意する。Pythonは3.14を使う。管理者権限が必要なら、その操作と理由を本人へ伝える。既存venvを勝手に削除しない。WSLでは `docs/windows.md` のuvによるvenv作成を使う。macOS/LinuxでPython 3.14が利用できる場合は次を実行する。

```bash
python3.14 -m venv venv
venv/bin/python -m pip install -r requirements.txt
```

## 2. 最初の情報を確認する

会話にある回答は再質問しない。不足する会社名（個人利用なら空欄可）、Bot名（既定Slack Bridge Assistant）、使用するAI（claudeまたはcodex）をまとめて確認する。秘密情報は質問しない。名前は利用者自身のものを使う。

非秘密情報だけを `scripts/configure.py --company ... --bot-name ... --provider ...` に渡してpreviewし、対象ファイルと差分を確認してから同じ引数へ `--apply` を付ける。文字列は実行環境に合わせて正しくクォートし、ユーザー入力をそのままシェルコードに挿入しない。

`.env` の会社名・Bot名が設定の正本となり、生成manifestのApp名・Bot表示名と `IDENTITY.md` の名前へ反映される。既存の口調・役割や秘密トークンを初期化しない。既に作成済みのSlack Appの表示名やアイコンは、本人がSlack管理画面でも更新する必要がある。

本人が全部を端末で入力したい場合は `venv/bin/python scripts/configure.py --interactive --apply` を案内する。AI自身が秘密入力用の端末を中継しない。

## 3. SlackとAIの本人操作を案内する

`config/slack-app-manifest.yaml` を使ったSlack App作成、ワークスペースへのインストール、`connections:write` を持つApp token発行、Bot招待を `docs/setup.md` に沿って案内する。

Bot/App tokenは、本人が自分のターミナルで次を実行して非表示入力する。値をチャット・コマンド引数・Gitへ貼らせない。AIは `.env` をcatしたり、秘密入力をツール経由で受け取ったりしない。

```bash
venv/bin/python scripts/configure.py --tokens --apply
```

CLIのログインも本人が同じmacOS/Linux/WSLユーザーで行う。既存の認証ファイルを別の環境へコピーしない。本人操作が残るときは、完了した設定と再開コマンドを伝えて待つ。

## 4. 診断・必要な修正・引き渡し

`venv/bin/python scripts/doctor.py --json` で診断する。doctorはローカル検査でありSlack認証成功を証明しない。失敗した項目に合わせてインストールや設定を直し、再実行する。Windows固有の実際のエラーでコード修正が必要なら、対象を絞って修正し、関連テストと `git diff --check` を実行する。既存の利用者変更を戻さない。

WindowsはPCやWSL・CLIの違いで修正が必要な場合があることを説明する。診断結果を根拠に、この利用者の環境に合わせて直す。秘密値を含むログ全体を要求・転記しない。

成功時は `venv/bin/python scripts/run.py` による起動とCtrl+Cによる停止を案内する。実行端末は起動したままにする。Slackへのテスト送信は本人が行うか、送信先と内容が明示された依頼がある場合だけ実行する。

完了報告はOS/WSL、選択したAI、ブランド設定、doctorの合否、未実施の本人操作・実接続確認を簡潔に記載する。自動テストだけでSlackからAIまで動いたと報告しない。
