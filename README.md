# Slack Bridge

自分の Slack から、手元の端末の Claude Code / Codex に仕事を依頼するための配布版です。
請求・会計や社内システムへの接続は含みません。利用者自身の Slack App と AI CLI のログインを使います。

## できること

- メンション・DMから依頼し、同じスレッドで会話を続ける
- 添付ファイルとスレッドの文脈をAIへ渡す
- 進捗・確認待ち・完了の返信をSlackで受け取る
- AIからの質問にSlackで回答する
- セッションを停止・再開し、複数スレッドを並行して処理する
- SQLiteで処理状態とキューを保存し、再起動後の会話を再開する
- `:ai-boot:` リアクションから、そのスレッドでAIに手伝えることを提案させる
- `IDENTITY.md` で名前・口調・役割を変える

既定のAIは Claude Code です。`.env` の `AI_WORKER_PROVIDER=codex` で Codex に切り替えられます。

## 最初の起動

macOS / Linux / WSL、Python **3.14**、tmux、bash、curl、ログイン済みの Claude Code または Codex が必要です。

```bash
git clone https://github.com/BRANCO-Inc/slack-bridge-public.git
cd slack-bridge-public
python3.14 -m venv venv
venv/bin/python -m pip install -r requirements.txt
venv/bin/python scripts/setup.py
```

1. 生成された `config/slack-app-manifest.yaml` で、自分のワークスペースに Slack App を作成します。
2. Appをインストールし、Bot tokenと `connections:write` を持つApp tokenを `.env` に設定します。setupが生成したローカル返信用トークンはそのまま残します。
3. 利用するチャンネルにBotを招待します。
4. 検査して起動します。

```bash
venv/bin/python scripts/doctor.py
venv/bin/python scripts/run.py
```

Slackで `@Slack Bridge Assistant テストです。短く返信してください。` と送り、スレッドに返事が来れば導入完了です。
起動中の端末が処理を担当します。終了は実行中のターミナルで `Ctrl+C` を押します。

画面ごとの操作は [導入手順](docs/setup.md)、動作の確認項目は [動作確認](docs/smoke-test.md) にあります。
Socket Modeを利用するため、外部公開するHTTPサーバーは不要です。[Slack公式ガイド](https://docs.slack.dev/apis/events-api/using-socket-mode/)

## 利用範囲と設定

このBridgeは、信頼できるチームの作業端末で使う前提です。AI workerは既存の実行仕様としてCLIの権限確認を省略して起動し、そのOSユーザーがアクセスできるファイルやコマンドを操作できます。Slackで依頼できる人と、実行端末のアクセス権を同じ信頼範囲に揃えてください。

`config/members.json` は呼び名とセッション操作の管理者を登録する名簿です。空でも動作し、依頼できる人を絞る許可リストではありません。Slack Connectの共有チャンネルは既定で受け付けません。

`.env`、`IDENTITY.md`、利用者名簿、生成manifest、実行時のDB・ログはGit管理の対象外です。更新時も自分の設定を保持できます。初期設定の正本は `templates/` にあります。

## 開発・更新

```bash
venv/bin/python -m pip install ruff
venv/bin/python -m unittest discover -v
venv/bin/python -m ruff check .
```

macOSとLinuxで同じテストをGitHub Actionsでも実行します。実Slackへの投稿やAI CLIの課金を伴うテストは自動実行しません。
更新はBridgeを停止して `git pull --ff-only`、依存関係のインストール、doctor、起動の順で行います。

[配布範囲と構成](docs/distribution.md)
