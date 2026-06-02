# Changelog

All notable changes to the `claude-langfuse` Claude Code plugin are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [0.4.0] - 2026-06-02

### Changed
- **Offline-first Stop hook (the real per-turn latency fix).** The Stop hook now
  runs `uv run --offline …` so uv resolves the environment from its cache only —
  it never refreshes its package index over the network mid-turn. That network
  refresh (occasional, ~20s+ on slow networks) was the cause of rare turns that
  stalled for 20+ seconds even for a tiny reply; the work *inside* the hook was
  always ~0.4s. A `|| uv run …` (online) fallback self-heals a cold cache. The
  online refresh now happens only during the SessionStart warmup, off the
  per-turn path.

### Renamed
- The `/langfuse-doctor` slash command is now **`/test`** (`commands/test.md`;
  fully qualified `/claude-langfuse:test`). The underlying `--doctor` mode is
  unchanged.

## [0.3.0] - 2026-06-01

### Added
- **`/langfuse-doctor` slash command** (`commands/langfuse-doctor.md`) and a
  `--doctor` mode in `bin/run_hook.py`. Prints a health report — system
  requirements (Python + langfuse SDK), `.env` config (set/missing `CC_*` vars,
  keys masked), identity, and a **live connectivity** check (synthetic trace) —
  ending in a `READY` / `NOT READY` verdict with exact next steps. Runs through
  the real uv runtime, so it works the same on macOS/Linux/Windows.
- **`CC_LANGFUSE_TIMEOUT`** (default 8s) — per-HTTP-call timeout on the Langfuse
  client.
- **`CC_LANGFUSE_FLUSH_TIMEOUT`** (default 5s) — hard cap on the end-of-turn
  drain.

### Changed
- **Bounded, non-blocking flush (latency fix).** The end-of-turn send no longer
  does an unbounded `flush()` + `shutdown()`. Both are replaced by a single
  time-capped drain on a daemon thread (`bounded_shutdown`, in the new
  `client.py`): the trace is waited on for at most `CC_LANGFUSE_FLUSH_TIMEOUT`
  seconds and the hook returns regardless, so a slow/unreachable Langfuse can
  never stall a turn (Stop hooks block the next user input). The redundant
  double-flush is gone. Worst case on a dead host: one dropped trace, capped
  wait — never a stalled turn.
- The Langfuse client is now built in one place (`client.build_client`) with an
  HTTP `timeout`, shared by the hook, the CLI `test`, and the doctor.

## [0.2.0] - 2026-06-01

### Changed
- **Cross-platform launcher.** Replaced the Unix-only `bin/run-hook.sh` with a
  single OS-agnostic hook command —
  `uv run --no-project --with "langfuse>=3.0,<4.0" bin/run_hook.py` — and a new
  cross-platform Python entry script `bin/run_hook.py`. The plugin now works on
  **Windows** as well as macOS/Linux.
- **Runtime simplified.** uv's cached per-requirements environment replaces the
  manual venv build, dep-marker, and background-build logic. `--no-project`
  ensures no `.venv` is created in the user's project directory. This also
  removes the first-run 30s-timeout failure mode.
- Added a **SessionStart `--warmup`** hook so uv's environment is resolved once
  per session before the first traced turn.

### Removed
- `bin/run-hook.sh` and its bash-only remediation messages for
  uv-missing / venv-build-failed. `uv` is now a documented hard prerequisite;
  the Python-side **config-incomplete** message is retained.

## [0.1.0] - 2026-06-01

Initial release. Repackages the `claude-code-langfuse-hook` tracer as a Claude
Code plugin that auto-registers its own Stop hook and runs in an isolated,
uv-managed Python environment — no `pip install` or manual settings edits.

### Added
- **Auto-registered Stop hook** via `hooks/hooks.json` — enabling the plugin is
  the only step; no editing of `~/.claude/settings.json`.
- **uv-managed runtime** (`bin/run-hook.sh`): builds and caches a private
  virtualenv with the `langfuse` SDK; uv can provision Python itself, so no
  system Python is required.
- **Fail-open runtime self-check** with throttled, plain-English remediation
  messages (once per session) for: `uv` missing, venv build failure, broken
  `langfuse` import, and incomplete `.env` configuration.
- **Backdated timestamps** — observations are pinned to the real transcript
  timestamps, so durations and ordering in Langfuse are accurate instead of
  collapsing to hook-fire time.
- **Tool error levels** — a tool whose result is an error is recorded at
  `ERROR` level with a status message.
- **Sub-agent (Task) nesting** — sidechain rows no longer open spurious
  top-level turns; sub-agent tool calls nest under a `Sub-agent (Task)` span.
- **Time-to-first-token** — `completion_start_time` set on the generation.

### Notes
- Reads `CC_*` configuration from the project's `.env` (walked up from the
  working directory); OS environment variables take precedence.
- Pinned to Langfuse SDK `>=3.0,<4.0` in the isolated venv. Backdating uses the
  SDK's OpenTelemetry internals with a graceful fallback to live spans if those
  internals are ever absent.
