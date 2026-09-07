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

**Claude Code / Codexに初回設定を任せられます。** macOS / Linuxで、またはWindowsのWSL2内で、このリポジトリを開いてください。

```bash
git clone https://github.com/BRANCO-Inc/slack-bridge-public.git
cd slack-bridge-public
claude
```

Codexを使う場合、最後の行は `codex` にします。AIに次のように依頼してください。

```text
Slack Bridgeを初回セットアップして。
必要な情報を順に聞き、環境の準備、会社名・Bot名の設定、診断まで進めて。
秘密トークンはチャットに貼らず、手元の端末で入力したい。
```

明示的に呼ぶ場合は、Claude Codeでは `/slack-bridge-setup`、Codexでは `$slack-bridge-setup` を使います。Skillはリポジトリ内に同梱されています。表示されなければ、このフォルダでAIを再起動するか「`.agents/skills/slack-bridge-setup/SKILL.md` を読んで実行して」と伝えてください。

会社名・Bot名・利用するAIを最初に設定すると、Slack App用manifestとAIの名前へ反映されます。Slack Appの作成、アカウントへのログイン、秘密トークンの入力は本人が行い、AIが手順を案内します。アイコンはSlack Appの管理画面で設定してください。

Windowsでは **WSL2が必要** です。BridgeとAI CLIはWSL内にインストールして実行します。[Windows導入手順](docs/windows.md) に、WSLの準備とPowerShellからの設定・起動方法があります。Windows標準のPythonだけでは動作しません。

**WindowsはPCの設定やWSL・CLIの違いにより、そのままでは動かない場合があります。各自のClaude Code / Codexに診断結果を渡し、自分の環境に合わせて適宜修正して使ってください。** 依頼文と確認項目もWindows手順に掲載しています。

AIを使わず進める場合も、[導入手順](docs/setup.md) の対話式セットアップを利用できます。実行に必要なのはPython **3.14**、tmux、bash、curl、ログイン済みのClaude CodeまたはCodexです。
起動後はSlackで自分のBotにメンションし、[動作確認](docs/smoke-test.md) を行ってください。起動中の端末が処理を担当します。終了は実行中のターミナルで `Ctrl+C` を押します。
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

macOSとLinuxで同じテストをGitHub Actionsでも実行します。WindowsではPowerShellの起動経路をテストし、WSL呼び出し先はテスト用に置き換えます。Windows実機でのWSL・Slack・AIの一連の動作確認を代替するものではありません。実Slackへの投稿やAI CLIの課金を伴うテストは自動実行しません。
更新はBridgeを停止して `git pull --ff-only`、依存関係のインストール、doctor、起動の順で行います。

[配布範囲と構成](docs/distribution.md)
