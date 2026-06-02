---
description: Set up Langfuse tracing for this project — asks for the required settings once and writes them to the project's .env (creating it if it doesn't exist).
allowed-tools: Bash, Read, AskUserQuestion
---

Configure the claude-langfuse plugin for THIS project. Collect the required
settings and write them into the project's `.env`.

Follow these steps:

1. **Find the target `.env`.** Walk up from the current working directory to the
   nearest existing `.env`; if none, use the nearest `.git` directory's `.env`;
   otherwise `./.env`. Tell me the path you'll write to. If a `.env` already
   exists there, read it and show me which `CC_*` variables are already set —
   **mask secret values** (e.g. `pk-l…xyz`). Only ask me for the ones that are
   missing, plus anything I say I want to change.

2. **Ask me for the values** (use the question tool; offer sensible defaults):
   - `CC_PROJECT_NAME` — slug for this project. Default to the repo/folder name.
   - `CC_LANGFUSE_BASE_URL` — the Langfuse host (e.g. `https://…`).
   - `CC_LANGFUSE_PUBLIC_KEY` — starts with `pk-lf-`.
   - `CC_LANGFUSE_SECRET_KEY` — starts with `sk-lf-`.

   `CC_TRACE_TO_LANGFUSE` should be `true` unless I say otherwise. Don't ask
   about the optional tuning vars (`CC_LANGFUSE_TIMEOUT`, etc.) unless I bring
   them up — the defaults are fine.

3. **Write the values** with the helper, which upserts idempotently (updates
   existing keys in place, appends new ones, creates the file if missing,
   preserves every other line, writes atomically). Pass the values as
   environment variables so the helper does the quoting/formatting — do not
   hand-edit the `.env` yourself. Locate the plugin's `bin/setup_env.py` via
   `$CLAUDE_PLUGIN_ROOT` (fall back to finding it under the installed
   claude-langfuse plugin directory if that variable isn't set):

   ```bash
   CC_TRACE_TO_LANGFUSE="true" \
   CC_PROJECT_NAME="<name>" \
   CC_LANGFUSE_BASE_URL="<url>" \
   CC_LANGFUSE_PUBLIC_KEY="<pk>" \
   CC_LANGFUSE_SECRET_KEY="<sk>" \
   uv run --offline --no-project --python 3.12 "${CLAUDE_PLUGIN_ROOT}/bin/setup_env.py" "<target-.env-path>" \
   || CC_TRACE_TO_LANGFUSE="true" CC_PROJECT_NAME="<name>" CC_LANGFUSE_BASE_URL="<url>" CC_LANGFUSE_PUBLIC_KEY="<pk>" CC_LANGFUSE_SECRET_KEY="<sk>" \
   uv run --no-project --python 3.12 "${CLAUDE_PLUGIN_ROOT}/bin/setup_env.py" "<target-.env-path>"
   ```

   (The helper only uses the Python standard library, so the `--offline` run
   succeeds with no package downloads; the `|| …` fallback covers a cold uv
   cache on a brand-new machine.)

4. **Confirm and finish.** Show the helper's summary (which masks secrets). If
   it warns that `.env` isn't gitignored, offer to add it. The hook reads `.env`
   fresh on the next turn, so no restart is needed. Offer to run
   `/claude-langfuse:test` now to verify tracing end-to-end.

Never print my secret keys back in full — always mask them.
