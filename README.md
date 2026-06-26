# Slack Bridge Public

Slack Bridge Public is a minimal Slack to Claude Code or Codex worker bridge.

## What Is Included

- Slack event intake
- Claude Code worker launch through tmux by default
- optional Codex switchback with `AI_WORKER_PROVIDER=codex`
- case reply command
- SQLite state
- minimal health check
- setup and doctor scripts
- editable `IDENTITY.md`

## Quick Start

POSIX / WSL:

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
venv/bin/python scripts/setup.py
venv/bin/python scripts/doctor.py
venv/bin/python scripts/run.py
```

Windows native:

```powershell
py -3 -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe scripts\setup.py
.\venv\Scripts\python.exe scripts\doctor.py
.\venv\Scripts\python.exe scripts\run.py
```

The default provider is Claude Code. Use `AI_WORKER_PROVIDER=codex` to switch
back to Codex. Set `CLAUDE_BIN` or `CODEX_BIN` only when the selected CLI is not
available on `PATH`.

Windows is easiest through WSL. Native Windows requires `TMUX_BIN` and
`SLACK_BRIDGE_SHELL_BIN` to point at executable absolute paths, plus the selected
AI worker CLI path when it is not on `PATH`.

See `docs/setup.md` and `docs/smoke-test.md` for the full setup flow.
