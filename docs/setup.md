# 導入手順

## AIに設定を任せる

このフォルダでClaude CodeまたはCodexを開き、「Slack Bridgeを初回セットアップして」と依頼します。Claude Codeでは `/slack-bridge-setup`、Codexでは `$slack-bridge-setup` からも呼び出せます。会社名・Bot名・利用するAIを回答すると、AIが環境を調べ、設定と診断を進めます。

SkillはClaude Code用の `.claude/skills/slack-bridge-setup/` とCodex用の `.agents/skills/slack-bridge-setup/` に同梱しています。グローバルへのインストールは不要です。[Claude CodeのSkill仕様](https://code.claude.com/docs/en/skills)、[CodexのSkill仕様](https://developers.openai.com/codex/skills)

Windowsは [Windows導入手順](windows.md) でWSL2を用意し、その中のリポジトリでAIを起動してください。Slack Appの作成・CLIへのログイン・秘密トークンの入力は本人が行います。秘密値をAIとのチャットに貼る必要はありません。

以下は対話式セットアップを自分で進める場合の手順です。

## 1. 端末を準備する

対応OSはmacOS、Linux、WindowsのWSL2です。Python 3.14、tmux、bash、curlを用意し、使うAI CLIに端末上で一度ログインしてください。Claude Codeが既定です。Codexを使う場合は、そのCLIへのログインが必要です。

```bash
git clone https://github.com/BRANCO-Inc/slack-bridge-public.git
cd slack-bridge-public
python3.14 -m venv venv
venv/bin/python -m pip install -r requirements.txt
venv/bin/python scripts/configure.py --interactive --apply
```

会社名・Bot名・AIの種類を順番に入力します。manifestの生成後は次のSlack App作成を進め、端末の案内に従ってBot/App tokenを非表示で入力してください。トークンを後で用意する場合は入力を空欄にし、作成後に `venv/bin/python scripts/configure.py --tokens --apply` を実行できます。

生成対象は `.env`、`.env.example`、`IDENTITY.md`、`config/members.json`、`config/slack-app-manifest.yaml` です。会社名・Bot名は `.env` に保持し、Bot名をmanifestと `IDENTITY.md` に反映します。ローカル返信用トークンは自動生成します。既存のトークンや他の設定は保持します。

ひな形だけを作る既存の `venv/bin/python scripts/setup.py` も利用できます。通常の再実行では既存ファイルを上書きしません。

## 2. Slack Appを作る

1. [Slack Apps](https://api.slack.com/apps) で **Create New App → From a manifest** を選び、利用するワークスペースを指定します。
2. YAML画面に、生成された `config/slack-app-manifest.yaml` の内容を貼り付けて作成します。
3. **OAuth & Permissions → Install to Workspace** からインストールします。表示された **Bot User OAuth Token** を、対話式セットアップのBot token欄へ入力します。
4. **Basic Information → App-Level Tokens** でトークンを作り、scopeに `connections:write` を指定します。生成した **App-Level Token** を対話式セットアップのApp token欄へ入力します。
5. **Socket Mode** が有効になっていることを確認します。DMを使う場合は **App Home → Messages Tab** と、そこからのメッセージ送信を有効にします。新規manifestにはこの設定を含めています。
6. 利用するSlackチャンネルに作成したBotを招待します。プライベートチャンネルでも招待が必要です。

これらは [Slack公式のSocket Modeセットアップ](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/) に沿った手順です。トークンの値をリポジトリやSlackメッセージへ貼らないでください。

## 3. 自分の設定を入れる

`.env` の `SLACK_BRIDGE_AUTH_TOKEN` はローカルのworker返信を認証するための値です。setupが自動生成するので、そのまま保持します。

| 設定 | 内容 |
| --- | --- |
| `SLACK_BOT_TOKEN` | 自分のSlack AppのBot token |
| `SLACK_APP_TOKEN` | `connections:write`を持つApp token |
| `AI_WORKER_PROVIDER` | `claude` または `codex` |
| `SLACK_BRIDGE_COMPANY` | 会社名。個人利用では空欄可 |
| `SLACK_BRIDGE_BOT_NAME` | Bot名。configureでmanifestとIDENTITYへ反映 |
| `CLAUDE_MODEL` | Claudeで使うモデル。既定は`sonnet` |
| `SLACK_BRIDGE_HOOK_PORT` | ローカル返信のポート。既定は`9111` |
| `SLACK_BRIDGE_DATA_ROOT` | 実行データの保存先を変える場合の絶対パス |

CLIが `PATH` にあれば `CLAUDE_BIN`、`CODEX_BIN`、`TMUX_BIN`、`SLACK_BRIDGE_SHELL_BIN` は空で構いません。指定する場合は、実行できるファイルの絶対パスを使います。
WSLではWindows側ではなくWSL内のパスを使ってください。

会社名・Bot名・利用するAIはconfigureで変更します。次は秘密情報を含まない設定例です。最初に差分を表示し、続けて反映します。

```bash
venv/bin/python scripts/configure.py --company 'Example Co' --bot-name 'Example Assistant' --provider codex
venv/bin/python scripts/configure.py --company 'Example Co' --bot-name 'Example Assistant' --provider codex --apply
```

`IDENTITY.md` の口調・役割は手編集できます。作成済みのSlack Appの表示名は、ローカルmanifestを更新するだけでは変わりません。Slack Appの管理画面でも名前を変更してください。アイコンも管理画面で設定します。

呼び名やセッション操作の管理者を登録する場合は `config/members.json` を編集します。

```json
{
  "members": [
    {"slack_user_id": "U0123456789", "cc_call": "担当者"}
  ]
}
```

この名簿は起動の許可リストではありません。空の `{"members": []}` でも利用できます。
workerは端末のOSユーザー権限で動くため、信頼できるチーム内で使用してください。

## 4. 検査して起動する

```bash
venv/bin/python scripts/doctor.py
venv/bin/python scripts/run.py
```

doctorは必要なPython、依存ライブラリ、CLI、設定、保存先、ポートを検査します。Slack APIの認証確認やファイルの作成はしません。Slackへの接続はrunで行います。

runがtmuxのworkerセッションとローカル返信サーバーを作成します。同じ設定で複数のBridgeを同時起動しないでください。
起動後は [動作確認](smoke-test.md) を行います。

## 保存先と更新

実行データはmacOSでは利用者ホーム内の `Library/Application Support/slack-bridge`、Linux/WSLでは `XDG_DATA_HOME/slack-bridge`、未指定なら利用者ホーム内の `.local/share/slack-bridge` に保存されます。コードと実行データは分離されています。

更新は実行中のBridgeを `Ctrl+C` で終了してから行います。

```bash
git pull --ff-only
venv/bin/python -m pip install -r requirements.txt
venv/bin/python scripts/setup.py
venv/bin/python scripts/doctor.py
venv/bin/python scripts/run.py
```

setupの通常実行は既存の設定を保持します。テンプレートの追加設定が必要になった場合は、更新説明を確認して自分の設定へ反映してください。

以前の版で `IDENTITY.md` や `config/members.json` を編集済みの場合、この更新ではGit管理から外れるため、そのままのpullが競合で止まることがあります。初回の更新は旧Bridgeを停止し、旧フォルダを保持したまま別フォルダへcloneしてsetupを実行し、旧フォルダから `.env`、`IDENTITY.md`、`config/members.json` をコピーしてください。data rootを同じ場所に保てば実行データも引き継げます。新しいフォルダでdoctorを通してから起動します。

## 起動できないとき

WindowsではPC・WSL・CLIの環境差により、そのままでは起動しない場合があります。各自のClaude Code / Codexに診断結果を渡し、必要な修正を行って使ってください。[Windows手順の修正依頼文](windows.md) も利用できます。秘密トークンやログ全体をチャットへ貼らないでください。

| 症状 | 確認すること |
| --- | --- |
| Pythonの構文エラー | Python 3.14でvenvを作成したか |
| CLIが見つからない | 選んだCLIがインストール済みで、端末から起動・ログインできるか |
| hook portが使用中 | 既に起動中のBridgeがないか。別アプリが使用中なら`.env`のポートを変更 |
| Slack接続の認証エラー | Bot/App tokenの取り違え、App tokenのscope、Appのインストール状況 |
| チャンネルで反応しない | Botの招待、メンション、manifestのイベント設定 |
| DMを送れない | App HomeのMessages Tabとメッセージ送信設定 |
