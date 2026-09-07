# Windows と WSL2

Slack Bridge は Windows の Python や Windows 側の AI CLI では動かしません。WSL2 の Ubuntu 内で、非 root ユーザーが Linux ファイルシステム上の checkout と Linux に入れた Claude Code または Codex を使います。`C:\` と `/mnt/c` の checkout・venv・実行データは対象外です。

Windows ごとに WSL、社内ポリシー、ネットワーク、AI CLI のログイン状態が異なるため、全 PC でそのまま動く保証はありません。この配布では実 WSL、Slack、AI ログインを通した E2E 動作を主張しません。失敗時は、WSL 内の自分の Claude Code または Codex に `scripts/doctor.py` の失敗行とこの文書を渡し、その PC だけの修正を進めてください。token、`.env`、ログ本文、Slack の本文は共有しません。

## 1. WSL2 と checkout を用意する

WSL 未導入なら、利用者自身が管理者として開いた PowerShell で次を実行し、Windows を再起動します。WSL の導入と再起動はこの launcher が行いません。

```powershell
wsl --install -d Ubuntu
```

Ubuntu を初めて起動したときは、root ではない Linux ユーザーを作成します。次に Ubuntu 内で Git と uv を用意し、Linux 側のホームディレクトリに clone します。

```bash
sudo apt update
sudo apt install -y git curl tmux
curl -LsSf https://astral.sh/uv/install.sh | sh
mkdir -p ~/src
git clone https://github.com/BRANCO-Inc/slack-bridge-public.git ~/src/slack-bridge-public
cd ~/src/slack-bridge-public
```

新しいシェルを開いて `uv --version` を確認します。launcher の setup は `uv venv --python 3.14 --seed venv` を実行するため、必要な Python 3.14 とpipは uv が取得します。

## 2. WSL 内で AI CLI にログインする

使う方を一つだけ、Ubuntu 内に導入してログインします。Windows 側にだけ導入した CLI は使いません。

Claude Code を使う場合:

```bash
curl -fsSL https://claude.ai/install.sh | bash
cd ~/src/slack-bridge-public
claude
```

Claude Code は起動時のブラウザ手順でログインします。Codex を使う場合は [Codex CLI の公式手順](https://developers.openai.com/codex/cli/) に従って Ubuntu 内へ導入し、同じ Ubuntu シェルでログインしてください。

このリポジトリでAIに「Slack Bridgeを初回セットアップして」と依頼すれば、同梱Skillで続けられます。Ubuntuのターミナルで自分で進める場合は、次を順番に実行します。PowerShellに戻る必要はありません。

```bash
bash -l scripts/wsl.sh setup
bash -l scripts/wsl.sh configure
bash -l scripts/wsl.sh doctor
```

doctorがすべて `ok` になったら `bash -l scripts/wsl.sh run` で起動します。次のPowerShell launcherも同じスクリプトを呼び出します。

## 3. PowerShell launcher を使う

Windows の PowerShell では、WSL checkout の UNC view を開きます。この view は同じ Linux ファイルシステムを見ており、`C:\` に別の checkout を作りません。`-Distro` は `wsl --list --verbose` に表示される WSL2 distribution 名と完全に一致させます。

以下の `Ubuntu` は distribution 名です。別名を使う場合は UNC path と `-Distro` の両方を同じ名前に置き換えます。

```powershell
cd \\wsl.localhost\Ubuntu\home\<user>\src\slack-bridge-public
```

```powershell
.\scripts\windows.ps1 -Action setup -Distro Ubuntu -ProjectPath /home/<user>/src/slack-bridge-public
.\scripts\windows.ps1 -Action configure -Distro Ubuntu -ProjectPath /home/<user>/src/slack-bridge-public
.\scripts\windows.ps1 -Action doctor -Distro Ubuntu -ProjectPath /home/<user>/src/slack-bridge-public
.\scripts\windows.ps1 -Action run -Distro Ubuntu -ProjectPath /home/<user>/src/slack-bridge-public
```

`setup` は WSL 内で Python 3.14 の `venv` を作り、requirements を導入し、テンプレートから初期ファイルを作ります。`configure` は `scripts/configure.py --interactive --apply` を起動し、company、bot 名、AI provider、secret token を入力します。`doctor` がすべて `ok` になってから `run` を実行してください。

Windows PowerShell 5.1 で実行ポリシーが script 実行を妨げるときは、machine policy を変更せず、この一回の PowerShell process だけで実行します。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows.ps1 -Action doctor -Distro Ubuntu -ProjectPath /home/<user>/src/slack-bridge-public
```

## 問題の切り分け

| 症状 | 対応 |
| --- | --- |
| `WSL was not found` | WSL2 と Ubuntu を導入し、再起動後にやり直します。 |
| distribution が見つからない、または WSL2 でない | `wsl --list --verbose` で名前と version 2 を確認し、`-Distro` を一致させます。 |
| `/mnt` または Windows path のエラー | `/home/<user>/...` に clone し直し、その Linux path を `-ProjectPath` に渡します。 |
| `uv is required` | Ubuntu 内で上の uv 導入を実行し、新しい Ubuntu シェルを開きます。 |
| `Project venv is missing` | `-Action setup` を先に実行します。Python の修復は Windows 側でなく WSL 内で行います。 |
| AI CLI が見つからない、または未ログイン | provider に選んだ CLI を Ubuntu 内へ導入し、その Ubuntu シェルでログインします。 |

自分の Claude Code または Codex へ依頼するときは、次を使えます。`<doctor の fail 行>` だけを貼り、token、`.env`、ログ本文、Slack 本文は入れません。

```text
Windows の WSL2 上で Slack Bridge を /home/<user>/src/slack-bridge-public に置いています。
Windows 側の Python と CLI は使わず、Ubuntu の非 root ユーザー、venv/bin/python、Linux 側の Claude Code または Codex を使う制約です。
`venv/bin/python scripts/doctor.py` の失敗行: <doctor の fail 行>
docs/windows.md に従い、この PC 固有の原因を診断して最小の修正手順を示してください。token、.env、ログ本文、Slack 本文を要求しないでください。
```

導入根拠は [Microsoft の WSL 導入手順](https://learn.microsoft.com/en-us/windows/wsl/install)、[uv の導入手順](https://docs.astral.sh/uv/getting-started/installation/)、[uv の Python 管理](https://docs.astral.sh/uv/guides/install-python/)、[Claude Code の WSL 導入手順](https://code.claude.com/docs/en/getting-started)、[Codex CLI の公式手順](https://developers.openai.com/codex/cli/) です。
