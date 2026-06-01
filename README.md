# claude-langfuse (Claude Code plugin)

Automatically traces every Claude Code turn to **Langfuse** — prompts,
responses, tool calls, token usage and cost, per user and per project. Install
the plugin once; it wires up its own Stop hook and runs in a private, isolated
Python environment. No `pip install`, no editing of settings files.

## Setup (3 steps)

1. **Add the plugin and install it**

   In Claude Code:
   ```
   /plugin marketplace add fawad71/claude-langfuse-plugin
   /plugin install claude-langfuse@claude-langfuse
   ```
   (Or install from a local folder — ask your platform team for the path.)

2. **Install `uv` once** *(one-time, per machine)*

   `uv` is a small tool that manages the plugin's private Python environment.
   Paste this into your Terminal, then restart Claude Code:
   ```
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```
   You can skip this — if `uv` is missing, the plugin shows you this exact line
   the first time it needs it. Nothing else breaks.

3. **Add your Langfuse settings to the project's `.env`**

   Copy the keys from [`.env.example`](.env.example) into your project's `.env`:
   ```
   CC_TRACE_TO_LANGFUSE=true
   CC_PROJECT_NAME=my-project
   CC_LANGFUSE_BASE_URL=https://langfuse.internal.example.com
   CC_LANGFUSE_PUBLIC_KEY=pk-lf-...
   CC_LANGFUSE_SECRET_KEY=sk-lf-...
   ```

That's it. Your sessions appear in Langfuse, grouped under **Sessions**.

## How it works

- The plugin registers a **Stop hook** ([`hooks/hooks.json`](hooks/hooks.json))
  that runs after each turn.
- The hook script ([`bin/run-hook.sh`](bin/run-hook.sh)) finds `uv`, builds a
  private virtualenv with the `langfuse` SDK (cached after the first run), and
  hands the turn to the vendored tracer in [`vendor/`](vendor/).
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

If anything goes wrong (no `uv`, broken environment, missing `.env` keys), the
hook **never blocks Claude Code**. It logs to `~/.claude/state/claude_langfuse.log`
and, at most once per session, shows a short plain-English message telling you
how to fix it.

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
