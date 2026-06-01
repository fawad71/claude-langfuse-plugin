#!/usr/bin/env bash
# Claude Code Stop-hook entry point for the claude-langfuse plugin.
#
# Responsibilities:
#   1. Find `uv` (the one-time setup tool that manages an isolated Python).
#   2. Build/reuse a private virtualenv with the `langfuse` SDK installed.
#   3. Self-check that the venv is healthy (langfuse importable).
#   4. Hand the hook payload to the vendored Python tracer.
#
# Design rule: FAIL-OPEN. Whatever goes wrong, we exit 0 so Claude Code is
# never blocked. When something needs the user's attention we print a short,
# plain-English message as the hook's JSON `systemMessage` — throttled so it
# shows at most once per session per problem.

# Note: intentionally NOT using `set -e` — a non-zero from any probe must not
# abort the script. We handle every failure explicitly and always exit 0.
set -u

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-"$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"}"
VENDOR_DIR="${PLUGIN_ROOT}/vendor"

# Where to keep the venv + throttle markers. Prefer the plugin's data dir
# (writable across marketplace installs); fall back to a dir beside the plugin.
DATA_DIR="${CLAUDE_PLUGIN_DATA:-"${PLUGIN_ROOT}/.data"}"
VENV_DIR="${DATA_DIR}/venv"
VENV_PY="${VENV_DIR}/bin/python"
WARN_DIR="${DATA_DIR}/warnings"
LOG_FILE="${HOME}/.claude/state/claude_langfuse.log"

DEP_SPEC="langfuse>=3.0,<4.0"
# Bump when DEP_SPEC changes so existing venvs get re-synced.
DEP_MARKER="${VENV_DIR}/.deps-v1-langfuse3"

mkdir -p "$DATA_DIR" "$WARN_DIR" "$(dirname "$LOG_FILE")" 2>/dev/null || true

# Read the whole hook payload from stdin once; we both inspect it (to key the
# throttle by session) and forward it verbatim to Python.
PAYLOAD="$(cat 2>/dev/null || true)"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >>"$LOG_FILE" 2>/dev/null || true
}

# Best-effort session id extraction (no jq dependency). Falls back to "global".
session_id() {
  local sid
  sid="$(printf '%s' "$PAYLOAD" \
    | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
    | head -n1)"
  [ -n "$sid" ] && printf '%s' "$sid" || printf 'global'
}

# Emit a plain-English message to the user, at most once per (reason, session).
# Also mirror it to the log. Always returns 0.
warn_user_once() {
  local reason="$1" message="$2"
  local sid marker
  sid="$(session_id)"
  # Make the marker filename filesystem-safe.
  marker="${WARN_DIR}/$(printf '%s_%s' "$reason" "$sid" | tr -c 'A-Za-z0-9_.-' '_')"

  log "remediation[$reason]: $message"

  if [ -f "$marker" ]; then
    return 0  # already told them this session — stay quiet
  fi
  : >"$marker" 2>/dev/null || true

  # Stop hooks read stdout as JSON; `systemMessage` surfaces to the user.
  # Prefer python3 for correct escaping; fall back to a minimal escaper.
  # All messages are single-line by design, so the fallback only needs to
  # escape backslashes and double quotes to emit valid JSON.
  if command -v python3 >/dev/null 2>&1; then
    MSG="$message" python3 -c 'import json,os;print(json.dumps({"systemMessage": os.environ["MSG"]}))' 2>/dev/null && return 0
  fi
  local esc
  esc="$(printf '%s' "$message" | tr '\n\r\t' '   ' | sed 's/\\/\\\\/g; s/"/\\"/g')"
  printf '{"systemMessage": "%s"}\n' "$esc"
  return 0
}

# Occasionally prune stale throttle markers so the dir doesn't grow forever.
find "$WARN_DIR" -type f -mtime +14 -delete 2>/dev/null || true

# --- 1. Locate uv -----------------------------------------------------------
UV_BIN=""
if command -v uv >/dev/null 2>&1; then
  UV_BIN="$(command -v uv)"
else
  for cand in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv"; do
    if [ -x "$cand" ]; then UV_BIN="$cand"; break; fi
  done
fi

if [ -z "$UV_BIN" ]; then
  warn_user_once "uv-missing" \
"Langfuse tracing needs a one-time setup tool called 'uv'. In your Terminal run:  curl -LsSf https://astral.sh/uv/install.sh | sh  — then restart Claude Code. Tracing is paused until then; your work is unaffected."
  exit 0
fi

# --- 2. Ensure the venv + langfuse (built in the BACKGROUND) ----------------
# A cold first build (fetch a managed Python + install langfuse) can take ~40s,
# which exceeds the Stop-hook timeout. If we built it synchronously here the
# hook would be killed mid-install every turn and never finish. Instead we
# kick the build off detached, return immediately, and start tracing on the
# first turn after it completes.
BUILD_LOG="${DATA_DIR}/build.log"
BUILD_LOCK="${VENV_DIR}.building"   # atomic dir-lock so only one build runs

if [ ! -x "$VENV_PY" ] || [ ! -f "$DEP_MARKER" ]; then
  if [ -d "$BUILD_LOCK" ]; then
    # A build is already running. If the lock is stale (>15 min) clear it so a
    # wedged build can be retried; otherwise just wait it out quietly.
    if find "$BUILD_LOCK" -maxdepth 0 -mmin +15 2>/dev/null | grep -q .; then
      rm -rf "$BUILD_LOCK" 2>/dev/null || true
    else
      log "setup: build already in progress — skipping this fire"
      exit 0
    fi
  fi

  if mkdir "$BUILD_LOCK" 2>/dev/null; then
    log "setup: starting background venv build at $VENV_DIR (uv=$UV_BIN)"
    # Detach so the build outlives this hook invocation. Pass paths via env to
    # avoid quoting issues. `uv venv` fetches a managed Python if needed.
    UV_BIN="$UV_BIN" VENV_DIR="$VENV_DIR" VENV_PY="$VENV_PY" \
    DEP_SPEC="$DEP_SPEC" DEP_MARKER="$DEP_MARKER" \
    BUILD_LOG="$BUILD_LOG" BUILD_LOCK="$BUILD_LOCK" \
    nohup bash -c '
      if "$UV_BIN" venv "$VENV_DIR" >>"$BUILD_LOG" 2>&1 \
         && "$UV_BIN" pip install --python "$VENV_PY" "$DEP_SPEC" >>"$BUILD_LOG" 2>&1; then
        : >"$DEP_MARKER"
        echo "setup: venv ready" >>"$BUILD_LOG"
      else
        echo "setup: venv build FAILED — see above" >>"$BUILD_LOG"
      fi
      rm -rf "$BUILD_LOCK"
    ' >/dev/null 2>&1 &
    disown 2>/dev/null || true
    warn_user_once "setup-started" \
"Langfuse tracing is doing a one-time background setup (~1 min). Tracing starts automatically once it finishes; your work is unaffected."
  fi
  exit 0  # can't trace yet — build is (now) running
fi

# --- 3. Self-check: langfuse importable -------------------------------------
if ! "$VENV_PY" -c "import langfuse" >/dev/null 2>&1; then
  # Wipe the marker so the next fire rebuilds from scratch.
  rm -f "$DEP_MARKER" 2>/dev/null || true
  warn_user_once "langfuse-import-failed" \
"Langfuse tracing's Python environment looks broken (cannot import 'langfuse'). It will rebuild automatically on the next turn. If this keeps happening, share the log at $LOG_FILE."
  exit 0
fi

# --- 4. Dispatch to the vendored tracer -------------------------------------
# The vendored package handles stdin parsing, .env config, transcript reading,
# and Langfuse emission. Its own stdout (e.g. a config-incomplete systemMessage)
# passes straight through to Claude Code.
printf '%s' "$PAYLOAD" | PYTHONPATH="$VENDOR_DIR" "$VENV_PY" -m claude_code_langfuse_hook hook
# Ignore the Python exit code on purpose — the hook is fail-open by contract.
exit 0
