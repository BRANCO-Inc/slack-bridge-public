#!/usr/bin/env bash
set -euo pipefail

action=${1:-}
project_root=$(pwd -P)

case "$project_root" in
  /mnt/*)
    printf '%s\n' 'The Slack Bridge checkout must not resolve to /mnt/.' >&2
    exit 2
    ;;
esac

case "$project_root" in
  /home/*) ;;
  *)
    printf '%s\n' 'The Slack Bridge checkout must be on the WSL Linux filesystem below /home.' >&2
    exit 2
    ;;
esac

if [ "$(id -u)" -eq 0 ]; then
  printf '%s\n' 'Run Slack Bridge as your WSL user, not root.' >&2
  exit 2
fi

require_venv() {
  if [ ! -x venv/bin/python ]; then
    printf '%s\n' 'Project venv is missing. Run -Action setup first.' >&2
    exit 2
  fi
}

case "$action" in
  setup)
    if ! command -v uv >/dev/null 2>&1; then
      printf '%s\n' 'uv is required in WSL. Install it as your WSL user, then retry.' >&2
      exit 2
    fi
    if [ ! -x venv/bin/python ]; then
      uv venv --python 3.14 --seed venv
    fi
    venv/bin/python -m pip install -r requirements.txt
    venv/bin/python scripts/setup.py
    ;;
  configure)
    require_venv
    venv/bin/python scripts/configure.py --interactive --apply
    ;;
  doctor)
    require_venv
    venv/bin/python scripts/doctor.py
    ;;
  run)
    require_venv
    venv/bin/python scripts/run.py
    ;;
  *)
    printf '%s\n' 'Action must be setup, configure, doctor, or run.' >&2
    exit 2
    ;;
esac
