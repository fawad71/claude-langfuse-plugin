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
   /plugin install claude-langfuse@kavak-gcc-tools
   ```

3. **Add your Langfuse settings to the project's `.env`**

   Copy the keys from [`.env.example`](.env.example) into your project's `.env`:
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
- Both run a single, OS-agnostic command:
  `uv run --no-project --with "langfuse>=3.0,<4.0" bin/run_hook.py`. `uv`
  provisions Python + the SDK from its cache (no project `.venv` is created),
  so the same command works on macOS, Linux, and Windows. The cross-platform
  entry script ([`bin/run_hook.py`](bin/run_hook.py)) loads the vendored tracer
  in [`vendor/`](vendor/) and hands it the turn. SessionStart runs it once in
  `--warmup` mode so the first real turn doesn't pay the cold-start cost.
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

OS environment variables always win over `.env`.

## Developing / testing

```
PYTHONPATH=vendor python -m pytest tests/ -q
```
