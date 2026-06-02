---
description: Diagnose the claude-langfuse plugin — checks uv, Python, the langfuse SDK, your .env config, identity, and live connectivity to Langfuse.
allowed-tools: Bash
---

Run the claude-langfuse self-diagnostic for me and report the result.

Do exactly this:

1. Confirm `uv` is installed by running `uv --version`. If that fails, tell me
   uv is the one hard prerequisite, point me to https://docs.astral.sh/uv/ for
   the install command for my OS, and stop here.

2. Run the doctor from my current project directory (so it picks up this
   project's `.env`). It provisions Python + the langfuse SDK via uv and prints
   a health report:

   ```
   uv run --no-project --python 3.12 --with "langfuse>=3.0,<4.0" "${CLAUDE_PLUGIN_ROOT}/bin/run_hook.py" --doctor
   ```

   If `${CLAUDE_PLUGIN_ROOT}` isn't set in this context, locate the plugin's
   `bin/run_hook.py` (under the claude-langfuse plugin directory) and use that
   absolute path instead.

3. Show me the full report verbatim, then give a one-line verdict: either
   "tracing is fully set up and working" or a concrete list of exactly what I
   need to fix (e.g. enable `CC_TRACE_TO_LANGFUSE`, add missing keys, install
   uv, or check the Langfuse URL/network).

Do not guess or fabricate any of the checks — only report what the command
actually prints.
