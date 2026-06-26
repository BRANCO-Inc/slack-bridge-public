#!/bin/bash
set -euo pipefail

usage() {
  local exit_code="${1:-1}"
  local output="/dev/stderr"
  if [ "$exit_code" -eq 0 ]; then
    output="/dev/stdout"
  fi
  {
    echo "Usage: case_reply.sh [--wait|--done|--note] <text|->"
    echo "       case_reply.sh --react <reaction_name>"
  } > "$output"
  exit "$exit_code"
}

MODE="reply"
COMPLETION_ACTION="done"
REACTION_NAME=""
TEXT=""
if [ "${1:-}" = "--react" ]; then
  MODE="reaction"
  REACTION_NAME="${2:-}"
  [ -n "$REACTION_NAME" ] || usage
  shift 2
elif [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  [ $# -eq 1 ] || usage
  usage 0
elif [ "${1:-}" = "--wait" ]; then
  COMPLETION_ACTION="wait"
  shift
elif [ "${1:-}" = "--done" ]; then
  shift
elif [ "${1:-}" = "--note" ] || [ "${1:-}" = "--continue" ]; then
  COMPLETION_ACTION="continue"
  shift
elif [[ "${1:-}" == --* ]]; then
  usage
fi

if [ "$MODE" = "reaction" ]; then
  [ $# -eq 0 ] || usage
elif [ "${1:-}" = "-" ]; then
  shift
  TEXT="$(cat)"
elif [ $# -lt 1 ]; then
  if [ -t 0 ]; then
    usage
  fi
  TEXT="$(cat)"
else
  TEXT="$1"
fi

if [ "$MODE" = "reply" ]; then
  [ -n "$TEXT" ] || usage
fi

BRIDGE_DIR="$(cd "$(dirname "$0")" && pwd)"

resolve_project_python() {
  if [ -n "${SLACK_BRIDGE_PYTHON_BIN:-}" ]; then
    echo "$SLACK_BRIDGE_PYTHON_BIN"
    return
  fi
  if [ -x "$BRIDGE_DIR/venv/bin/python" ]; then
    echo "$BRIDGE_DIR/venv/bin/python"
    return
  fi
  if [ -x "$BRIDGE_DIR/venv/Scripts/python.exe" ]; then
    echo "$BRIDGE_DIR/venv/Scripts/python.exe"
    return
  fi
  echo "$BRIDGE_DIR/venv/bin/python"
}

SLACK_BRIDGE_PYTHON_BIN="$(resolve_project_python)"

if [ ! -x "$SLACK_BRIDGE_PYTHON_BIN" ]; then
  echo "Slack Bridge project venv python is required: $SLACK_BRIDGE_PYTHON_BIN" >&2
  echo "Create / repair the project venv before running case_reply.sh; system Python fallback is disabled." >&2
  exit 1
fi

resolve_instance() {
  SLACK_BRIDGE_INSTANCE_RAW="${SLACK_BRIDGE_INSTANCE:-}" "$SLACK_BRIDGE_PYTHON_BIN" - <<'PY'
import os
import re
import sys

raw = os.environ.get("SLACK_BRIDGE_INSTANCE_RAW", "")
value = raw.strip().lower()
if not value:
    print("")
    raise SystemExit(0)
if not re.fullmatch(r"^[a-z0-9][a-z0-9_-]*$", value):
    raise SystemExit(
        "SLACK_BRIDGE_INSTANCE must match ^[a-z0-9][a-z0-9_-]*$ "
        f"(received: {raw!r})"
    )
print(value)
PY
}

resolve_data_root() {
  "$SLACK_BRIDGE_PYTHON_BIN" - <<'PY'
import os
from pathlib import Path

raw = os.environ.get("SLACK_BRIDGE_DATA_ROOT", "").strip()
if raw:
    value = raw
else:
    profile = (os.environ.get("SLACK_BRIDGE_PROFILE", "") or "public").strip().lower()
    if profile in {"public", "core", "client"}:
        if os.name == "nt":
            base = os.environ.get("LOCALAPPDATA", "").strip() or str(
                Path.home() / "AppData" / "Local"
            )
            value = str(Path(base) / "slack-bridge")
        else:
            value = str(Path.home() / "Library" / "Application Support" / "slack-bridge")
    else:
        raise SystemExit(
            "SLACK_BRIDGE_PROFILE must be one of "
            "['client', 'core', 'public'] "
            f"(received: {profile!r})"
        )
if not Path(value).is_absolute():
    raise SystemExit(
        "SLACK_BRIDGE_DATA_ROOT must be an absolute path "
        f"(received: {raw!r})"
    )
print(value)
PY
}

resolve_hook_port() {
  SLACK_BRIDGE_INSTANCE_NORMALIZED="$INSTANCE" \
  SLACK_BRIDGE_HOOK_PORT_RAW="${SLACK_BRIDGE_HOOK_PORT:-}" \
  "$SLACK_BRIDGE_PYTHON_BIN" - <<'PY'
import hashlib
import os
import sys

instance = os.environ.get("SLACK_BRIDGE_INSTANCE_NORMALIZED", "")
raw = os.environ.get("SLACK_BRIDGE_HOOK_PORT_RAW", "").strip()
if raw:
    try:
        port = int(raw)
    except ValueError as exc:
        raise SystemExit(
            f"SLACK_BRIDGE_HOOK_PORT must be an integer (received: {raw!r})"
        ) from exc
    if not 1 <= port <= 65535:
        raise SystemExit(
            f"SLACK_BRIDGE_HOOK_PORT must be between 1 and 65535 (received: {port})"
        )
    print(port)
    raise SystemExit(0)
if not instance:
    print(9111)
    raise SystemExit(0)
digest = hashlib.sha256(instance.encode("utf-8")).hexdigest()
print(10000 + (int(digest[:8], 16) % 50000))
PY
}

bridge_auth_account() {
  SLACK_BRIDGE_INSTANCE_NORMALIZED="$INSTANCE" "$SLACK_BRIDGE_PYTHON_BIN" - <<'PY'
import os

instance = os.environ.get("SLACK_BRIDGE_INSTANCE_NORMALIZED", "")
suffix = ""
if instance:
    suffix = "_" + instance.upper().replace("-", "_")
print("SLACK_BRIDGE_AUTH_TOKEN" + suffix)
PY
}

resolve_bridge_auth_token() {
  local account
  account="$(bridge_auth_account)"
  SLACK_BRIDGE_AUTH_ACCOUNT="$account" "$SLACK_BRIDGE_PYTHON_BIN" - <<'PY'
import os

account = os.environ["SLACK_BRIDGE_AUTH_ACCOUNT"]
print(os.environ.get(account, "").strip())
PY
}

INSTANCE="$(resolve_instance)"
DATA_ROOT="$(resolve_data_root)"
export SLACK_BRIDGE_DATA_ROOT="$DATA_ROOT"
if [ -n "$INSTANCE" ]; then
  export SLACK_BRIDGE_INSTANCE="$INSTANCE"
  DATA_DIR="$DATA_ROOT/instances/$INSTANCE"
else
  unset SLACK_BRIDGE_INSTANCE
  DATA_DIR="$DATA_ROOT"
fi

RUNTIME_DIR="$DATA_DIR/runtime"
TMP_DIR="$RUNTIME_DIR/tmp"
HOOK_PORT="$(resolve_hook_port)"
BRIDGE_BASE_URL="${BRIDGE_BASE_URL:-http://127.0.0.1:$HOOK_PORT}"
TURN_CONTEXT_FILE="${CC_TURN_CONTEXT_FILE:-}"
MAX_ATTEMPTS="${CASE_REPLY_MAX_ATTEMPTS:-3}"
ATTEMPTS_MADE=0
CASE_REPLY_CONNECT_TIMEOUT="${CASE_REPLY_CONNECT_TIMEOUT:-3}"
CASE_REPLY_MAX_TIME="${CASE_REPLY_MAX_TIME:-20}"

mkdir -p "$TMP_DIR"
export TMPDIR="$TMP_DIR"

context_field() {
  local field="$1"
  [ -n "$TURN_CONTEXT_FILE" ] || return 0
  [ -f "$TURN_CONTEXT_FILE" ] || return 0
  "$SLACK_BRIDGE_PYTHON_BIN" - "$TURN_CONTEXT_FILE" "$field" <<'PY'
import json
import sys

path, field = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as handle:
    payload = json.load(handle)
print(payload.get(field, ""))
PY
}

CONTEXT_CASE_ID="$(context_field case_id)"
CONTEXT_PANE_ID="$(context_field pane_id)"
CONTEXT_WINDOW_NAME="$(context_field window_name)"
CONTEXT_TURN_ID="$(context_field turn_id)"
CONTEXT_SESSION_ID="$(context_field session_id)"

CASE_ID="${CONTEXT_CASE_ID:-${CC_CASE_ID:-}}"
PANE_ID="${CONTEXT_PANE_ID:-${CC_PANE_ID:-}}"
WINDOW_NAME="${CONTEXT_WINDOW_NAME:-${CC_WINDOW_NAME:-}}"
TURN_ID="${CONTEXT_TURN_ID:-${CC_TURN_ID:-}}"
SESSION_ID="${CONTEXT_SESSION_ID:-${SLACK_BRIDGE_WORKER_SESSION_ID:-}}"

[ -n "$CASE_ID" ] || { echo "CC_CASE_ID not set and turn context missing case_id" >&2; exit 1; }
[ -n "$TURN_ID" ] || { echo "turn_id not set and turn context missing turn_id" >&2; exit 1; }
[ -n "$SESSION_ID" ] || { echo "SLACK_BRIDGE_WORKER_SESSION_ID is required" >&2; exit 1; }

if [ "$MODE" = "reaction" ]; then
  if [ -n "${CC_REACTION_REQUEST_ID:-}" ]; then
    REQUEST_ID="$CC_REACTION_REQUEST_ID"
  else
    REQUEST_ID="$(
      CASE_ID="$CASE_ID" TURN_ID="$TURN_ID" REACTION_NAME="$REACTION_NAME" "$SLACK_BRIDGE_PYTHON_BIN" - <<'PY'
import hashlib
import os

seed = f"{os.environ['CASE_ID']}:{os.environ['TURN_ID']}:react:{os.environ['REACTION_NAME']}"
print(f"reaction_{hashlib.sha256(seed.encode('utf-8')).hexdigest()}")
PY
    )"
  fi
elif [ -n "${CC_REPLY_REQUEST_ID:-}" ]; then
  REQUEST_ID="$CC_REPLY_REQUEST_ID"
else
  REQUEST_ID="$(
    CASE_ID="$CASE_ID" TURN_ID="$TURN_ID" COMPLETION_ACTION="$COMPLETION_ACTION" TEXT="$TEXT" "$SLACK_BRIDGE_PYTHON_BIN" - <<'PY'
import hashlib
import os

seed = (
    f"{os.environ['CASE_ID']}:{os.environ['TURN_ID']}:"
    f"{os.environ['COMPLETION_ACTION']}:{os.environ['TEXT']}"
)
print(f"reply_{hashlib.sha256(seed.encode('utf-8')).hexdigest()}")
PY
  )"
fi

request_path() {
  if [ "$MODE" = "reaction" ]; then
    printf '%s\n' "/bridge/case_reaction"
  else
    printf '%s\n' "/bridge/case_reply"
  fi
}

build_reply_payload() {
  local attempt="$1"
  local final_attempt="$2"
  TEXT="$TEXT" \
  CC_CASE_ID="$CASE_ID" \
  TURN_ID="$TURN_ID" \
  WINDOW_NAME="$WINDOW_NAME" \
  PANE_ID="$PANE_ID" \
  REPLY_REQUEST_ID="$REQUEST_ID" \
  COMPLETION_ACTION="$COMPLETION_ACTION" \
  SESSION_ID="$SESSION_ID" \
  ATTEMPT="$attempt" \
  MAX_ATTEMPTS="$MAX_ATTEMPTS" \
  FINAL_ATTEMPT="$final_attempt" \
  "$SLACK_BRIDGE_PYTHON_BIN" - <<'PY'
import json
import os

payload = {
    "reply_request_id": os.environ["REPLY_REQUEST_ID"],
    "case_id": os.environ["CC_CASE_ID"],
    "turn_id": os.environ["TURN_ID"],
    "text": os.environ["TEXT"],
    "completion_action": os.environ["COMPLETION_ACTION"],
    "attempt": int(os.environ["ATTEMPT"]),
    "max_attempts": int(os.environ["MAX_ATTEMPTS"]),
    "final_attempt": os.environ["FINAL_ATTEMPT"] == "1",
}
if os.environ.get("WINDOW_NAME"):
    payload["window_name"] = os.environ["WINDOW_NAME"]
if os.environ.get("PANE_ID"):
    payload["pane_id"] = os.environ["PANE_ID"]
if os.environ.get("SESSION_ID"):
    payload["session_id"] = os.environ["SESSION_ID"]
print(json.dumps(payload, ensure_ascii=False))
PY
}

build_reaction_payload() {
  local attempt="$1"
  local final_attempt="$2"
  CC_CASE_ID="$CASE_ID" \
  TURN_ID="$TURN_ID" \
  WINDOW_NAME="$WINDOW_NAME" \
  PANE_ID="$PANE_ID" \
  REACTION_REQUEST_ID="$REQUEST_ID" \
  REACTION_NAME="$REACTION_NAME" \
  SESSION_ID="$SESSION_ID" \
  ATTEMPT="$attempt" \
  MAX_ATTEMPTS="$MAX_ATTEMPTS" \
  FINAL_ATTEMPT="$final_attempt" \
  "$SLACK_BRIDGE_PYTHON_BIN" - <<'PY'
import json
import os

payload = {
    "reaction_request_id": os.environ["REACTION_REQUEST_ID"],
    "case_id": os.environ["CC_CASE_ID"],
    "turn_id": os.environ["TURN_ID"],
    "reaction_name": os.environ["REACTION_NAME"],
    "attempt": int(os.environ["ATTEMPT"]),
    "max_attempts": int(os.environ["MAX_ATTEMPTS"]),
    "final_attempt": os.environ["FINAL_ATTEMPT"] == "1",
}
if os.environ.get("WINDOW_NAME"):
    payload["window_name"] = os.environ["WINDOW_NAME"]
if os.environ.get("PANE_ID"):
    payload["pane_id"] = os.environ["PANE_ID"]
if os.environ.get("SESSION_ID"):
    payload["session_id"] = os.environ["SESSION_ID"]
print(json.dumps(payload, ensure_ascii=False))
PY
}

build_payload() {
  if [ "$MODE" = "reaction" ]; then
    build_reaction_payload "$@"
  else
    build_reply_payload "$@"
  fi
}

REQUEST_PATH="$(request_path)"
HEADER_FILE="$(mktemp "$TMP_DIR/case_reply.headers.XXXXXX")"
BODY_FILE="$(mktemp "$TMP_DIR/case_reply.body.XXXXXX")"
ERROR_FILE="$(mktemp "$TMP_DIR/case_reply.error.XXXXXX")"
cleanup() {
  rm -f "$HEADER_FILE" "$BODY_FILE" "$ERROR_FILE"
}
trap cleanup EXIT

CURL_ARGS=(
  -H "Content-Type: application/json"
)
AUTH_TOKEN="$(resolve_bridge_auth_token)"
if [ -z "$AUTH_TOKEN" ]; then
  echo "SLACK_BRIDGE_AUTH_TOKEN is required before sending a case reply" >&2
  exit 1
fi
CURL_ARGS+=(-H "X-Bridge-Token: $AUTH_TOKEN")

retry_delay() {
  local attempt="$1"
  local retry_after
  retry_after="$(awk 'BEGIN{IGNORECASE=1} /^Retry-After:/ {gsub("\r","",$2); print $2; exit}' "$HEADER_FILE")"
  if [[ "$retry_after" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$retry_after"
    return
  fi

  local delay=$((1 << (attempt - 1)))
  if [ "$delay" -gt 8 ]; then
    delay=8
  fi
  printf '%s\n' "$delay"
}

for i in $(seq 1 "$MAX_ATTEMPTS"); do
  ATTEMPTS_MADE="$i"
  : >"$HEADER_FILE"
  : >"$BODY_FILE"
  : >"$ERROR_FILE"
  FINAL_ATTEMPT=0
  if [ "$i" -ge "$MAX_ATTEMPTS" ]; then
    FINAL_ATTEMPT=1
  fi
  PAYLOAD="$(build_payload "$i" "$FINAL_ATTEMPT")"

  if HTTP_CODE="$(
    curl -sS --connect-timeout "$CASE_REPLY_CONNECT_TIMEOUT" --max-time "$CASE_REPLY_MAX_TIME" \
      -D "$HEADER_FILE" -o "$BODY_FILE" -w '%{http_code}' -X POST "$BRIDGE_BASE_URL$REQUEST_PATH" \
      "${CURL_ARGS[@]}" \
      -d "$PAYLOAD" 2>"$ERROR_FILE"
  )"; then
    CURL_EXIT=0
  else
    CURL_EXIT=$?
    HTTP_CODE="000"
  fi

  if [ "$CURL_EXIT" -eq 0 ] && [ "$HTTP_CODE" -ge 200 ] 2>/dev/null && [ "$HTTP_CODE" -lt 300 ] 2>/dev/null; then
    exit 0
  fi

  SHOULD_RETRY=0
  if [ "$CURL_EXIT" -ne 0 ]; then
    SHOULD_RETRY=1
  elif [ "$HTTP_CODE" -eq 429 ] 2>/dev/null || [ "$HTTP_CODE" -ge 500 ] 2>/dev/null; then
    SHOULD_RETRY=1
  fi

  echo "[case_reply] Attempt $i failed (HTTP ${HTTP_CODE:-000}, request_id=$REQUEST_ID, turn_id=$TURN_ID)" >&2
  if [ -s "$ERROR_FILE" ]; then
    cat "$ERROR_FILE" >&2
  elif [ -s "$BODY_FILE" ]; then
    cat "$BODY_FILE" >&2
    echo >&2
  fi

  if [ "$SHOULD_RETRY" -eq 1 ] && [ "$i" -lt "$MAX_ATTEMPTS" ]; then
    sleep "$(retry_delay "$i")"
    continue
  fi

  break
done

echo "[case_reply] Failed after $ATTEMPTS_MADE/$MAX_ATTEMPTS attempt(s) (request_id=$REQUEST_ID, turn_id=$TURN_ID)" >&2
exit 1
