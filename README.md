# claude-langfuse (Claude Code plugin)

Automatically traces every Claude Code turn to **Langfuse** — prompts,
responses, tool calls, token usage and cost, per user and per project. Install
the plugin once; it wires up its own Stop hook. Works on **macOS, Linux, and
Windows**.

## Setup (3 steps)

1. **Install `uv` once** *(prerequisite, one-time per machine)*

   `uv` provisions the plugin's Python runtime + the `langfuse` SDK. It is
   required on every platform.

   - **macOS / Linux:**
     ```
     curl -LsSf https://astral.sh/uv/install.sh | sh
     ```
   - **Windows (PowerShell):**
     ```
     winget install --id=astral-sh.uv -e
     ```
     (or `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`)

   Restart Claude Code after installing so `uv` is on its PATH.

2. **Add the plugin and install it**

   In Claude Code (use the local folder, or your team's internal GitLab
   marketplace URL):
   ```
   /plugin marketplace add /path/to/claude-langfuse-plugin-repo
   /plugin install claude-langfuse@claude-langfuse
   ```

3. **Add your Langfuse settings to the project's `.env`**

   Easiest: run **`/setup`** in the project and answer the prompts — it writes
   the values into your project's `.env` (creating it if needed, updating it in
   place if it exists), then you can run `/test` to confirm.

   Or do it by hand — copy the keys from [`.env.example`](.env.example) into
   your project's `.env`:
   ```
   CC_TRACE_TO_LANGFUSE=true
   CC_PROJECT_NAME=my-project
   CC_LANGFUSE_BASE_URL=https://langfuse.internal.example.com
   CC_LANGFUSE_PUBLIC_KEY=pk-lf-...
   CC_LANGFUSE_SECRET_KEY=sk-lf-...
   ```

That's it. Your sessions appear in Langfuse, grouped under **Sessions**. The
first turn of a session warms up the runtime; tracing is fully active from then
on.

## How it works

- The plugin registers a **Stop hook** and a **SessionStart warmup hook**
  ([`hooks/hooks.json`](hooks/hooks.json)).
- Both run a single, OS-agnostic command via `uv`, which provisions Python +
  the SDK from its cache (no project `.venv` is created), so the same command
  works on macOS, Linux, and Windows. The cross-platform entry script
  ([`bin/run_hook.py`](bin/run_hook.py)) loads the vendored tracer in
  [`vendor/`](vendor/) and hands it the turn.
- **Both hooks run `uv run --offline …`** (cache-only) so uv never phones home —
  not at session start (warmup), not mid-turn (Stop). This makes both the
  session-start and per-turn cost deterministic (~0.3–0.5s once the cache
  exists). Each command has a `|| uv run …` online fallback that self-heals a
  cold cache, so the one-time SDK/runtime download still happens automatically
  on first install — it just never blocks the hot path afterward.
- **SessionStart** runs `… --warmup` to import the SDK so the very first Stop
  doesn't pay the import cost; **Stop** emits the traces.
- The tracer reads `CC_*` settings from your project's `.env` (walked up from
  the working directory), reconstructs each turn from the session transcript,
  and emits one Langfuse trace per turn:

  ```
  trace  "Claude Code - Turn N"   (session_id, user_id, tags)
    └─ span  "Claude Code - Turn N"
        ├─ generation  "Claude Response"   (model, I/O, token usage, cost)
        ├─ tool  "Tool: <name>"            (input/output; ERROR level if it failed)
        └─ agent "Sub-agent (Task)"        (nested sub-agent tool calls)
  ```

### What's enhanced over a basic hook

- **Real timelines (backdated):** observations are pinned to the actual
  timestamps in the transcript, so durations and ordering are accurate instead
  of collapsing to the moment the hook fired.
- **Failures stand out:** a tool whose result is an error is recorded at
  `ERROR` level.
- **Sub-agents are nested:** Task sub-agent activity shows as its own subtree
  instead of polluting the main turn sequence.
- **Time-to-first-token:** the generation carries `completion_start_time`.

### Checking your setup

Not sure the hook is wired up or that the system requirements are met? Run the
built-in diagnostic from inside Claude Code:

```
/claude-langfuse:test
```

(or just `/test` if the name is unambiguous in your setup). It checks, in order: **system requirements** (Python + the `langfuse` SDK via
uv), your **`.env` config** (which `CC_*` vars are set / missing, keys masked),
your **identity** (`git config user.email`), and **live connectivity** (it
sends a synthetic trace and confirms Langfuse accepted it within the flush cap).
It ends with a clear `READY` / `NOT READY` verdict and exactly what to fix.

You can also run it directly (same command the slash command uses):

```
uv run --no-project --python 3.12 --with "langfuse>=3.0,<4.0" "$CLAUDE_PLUGIN_ROOT/bin/run_hook.py" --doctor
```

### Latency: never blocks a turn

The Stop hook runs synchronously (Claude Code waits for it before the next
turn), so two things are kept off the per-turn critical path:

- **uv never refreshes its index on the hot path.** Both hooks run `--offline`
  (cache-only, ~0.05–0.1s of uv overhead); uv's occasional ~20–40s network
  re-resolution can only happen via the online fallback on a genuinely cold
  cache (first install). This was the cause of both the rare "a short turn took
  20+ seconds" stall *and* slow session starts.
- **The network send is time-capped.** The trace is drained on a daemon thread
  and waited on for at most `CC_LANGFUSE_FLUSH_TIMEOUT` seconds (default 5),
  with each HTTP call capped at `CC_LANGFUSE_TIMEOUT` (default 8). If Langfuse is
  slow or unreachable the hook returns at the cap — at worst a single trace is
  dropped, never your turn.

Warm steady-state cost: uv launch + SDK import + ~0.4s of work ≈ **under a
second** per turn. (The first turn of the very first session pays a one-time
runtime download, which the warmup hook absorbs.)

### Fail-open by design

The hook **never blocks Claude Code**. The Python tracer swallows all errors,
always exits 0, and logs to `~/.claude/state/claude_langfuse.log` (on Windows:
`%USERPROFILE%\.claude\state\claude_langfuse.log`). If tracing is enabled but
`.env` keys are missing, it shows a one-time plain-English message naming the
missing variables.

The one hard prerequisite is **`uv`** (step 1). If `uv` isn't installed/on PATH,
the hook command can't start and Claude Code surfaces a generic hook error —
install `uv` and restart.

### Troubleshooting

0. **Run `/claude-langfuse:test` first** — it pinpoints most issues (missing uv,
   incomplete `.env`, unreachable host) in one shot.
1. Check the log (last lines):
   - macOS/Linux: `tail -n 20 ~/.claude/state/claude_langfuse.log`
   - Windows (PowerShell): `Get-Content $env:USERPROFILE\.claude\state\claude_langfuse.log -Tail 20`
2. Interpreting it:
   - `Emitted turn … / Processed N turns` → working; if you don't see traces it's
     a Langfuse **UI filter** (check project, time range — traces are backdated
     to the real turn time — and `environment: default`).
   - `Tracing not enabled` / `missing fields: CC_…` → fix your project's `.env`.
   - Empty / no log → the hook isn't running. Confirm the plugin is enabled
     (`/plugin`) and that `uv --version` works in your terminal.
3. **`uv` works in your terminal but the hook says it can't find `uv`** (common
   on **Windows**, and possible on macOS/Linux). A running process keeps the
   PATH it was launched with, so if you installed `uv` *after* starting Claude
   Code, its hooks won't see `uv` yet. Fixes, in order:
   1. **Fully quit and reopen Claude Code** (restart the app, not just a new
      terminal). If you launch it from a terminal, open a fresh one where
      `uv --version` works, then start Claude Code from there.
   2. Windows only: if it persists, **reboot** — Start-menu apps inherit PATH
      from Explorer, which refreshes on logon/reboot.
   3. Durable fix — add uv's bin dir to the PATH Claude Code passes to hooks, in
      `~/.claude/settings.json` (Windows: `%USERPROFILE%\.claude\settings.json`):
      ```json
      { "env": { "PATH": "C:\\Users\\<you>\\.local\\bin;${PATH}" } }
      ```
      (macOS/Linux: `{ "env": { "PATH": "$HOME/.local/bin:${PATH}" } }`.)
      `~/.local/bin` is uv's default install location. Restart Claude Code after.

## Configuration reference

| Variable | Required | Meaning |
|---|---|---|
| `CC_TRACE_TO_LANGFUSE` | yes | `true` to enable tracing |
| `CC_PROJECT_NAME` | yes | project slug tagged on every trace |
| `CC_LANGFUSE_BASE_URL` | yes | Langfuse host |
| `CC_LANGFUSE_PUBLIC_KEY` | yes | Langfuse public key (`pk-lf-…`) |
| `CC_LANGFUSE_SECRET_KEY` | yes | Langfuse secret key (`sk-lf-…`) |
| `CC_LANGFUSE_DEBUG` | no | `true` for verbose logging |
| `CC_LANGFUSE_MAX_CHARS` | no | truncation cap (default 20000) |
| `CC_LANGFUSE_TIMEOUT` | no | per-HTTP-call timeout in seconds (default 8) |
| `CC_LANGFUSE_FLUSH_TIMEOUT` | no | hard cap in seconds on the end-of-turn flush (default 5) |

OS environment variables always win over `.env`.

## Developing / testing

```
PYTHONPATH=vendor python -m pytest tests/ -q
```
