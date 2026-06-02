#!/usr/bin/env python3
"""Cross-platform entry point for the claude-langfuse Stop hook.

Invoked by Claude Code via `uv run` (see hooks/hooks.json), which works
identically on Windows, macOS, and Linux and provisions Python + the langfuse
SDK from uv's cache. This script just wires the vendored package onto the path
and hands control to it.

Three modes:
  (default)   run the Stop hook: read the payload on stdin, resolve `.env`,
              emit one Langfuse trace per turn.
  --warmup    import langfuse and exit. Used by the SessionStart hook so uv's
              environment is resolved/cached before the first real turn, which
              avoids a cold-start stall on the first Stop event.
  --doctor    print a health report (system requirements, config, identity,
              live connectivity). Surfaced via the `/langfuse-doctor` command.

Fail-open by contract: any error is swallowed and we always exit 0, so the hook
can never block Claude Code. (`--doctor` is diagnostic, so it propagates its
own exit code: 0 = ready, 1 = something to fix.)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _plugin_root() -> Path:
    """Plugin install dir: CLAUDE_PLUGIN_ROOT if set, else this file's parent's parent."""
    env = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def main() -> int:
    # Put the vendored package on the import path. `vendor/` sits next to `bin/`.
    vendor = _plugin_root() / "vendor"
    sys.path.insert(0, str(vendor))

    args = sys.argv[1:]
    warmup = "--warmup" in args
    doctor = "--doctor" in args

    try:
        if doctor:
            # Diagnostic mode — print a report and return its real exit code.
            from claude_code_langfuse_hook.doctor import run_doctor
            return run_doctor()
        if warmup:
            # Touch the SDK so uv resolves + caches the environment now.
            import langfuse  # noqa: F401
            return 0
        from claude_code_langfuse_hook.hook import run
        return run()
    except Exception:
        # Last-resort guard: never let an import/runtime error surface to
        # Claude Code. Details (if any) are logged by the package itself.
        return 1 if doctor else 0


if __name__ == "__main__":
    # Fail-open for the hook (always exit 0); the doctor propagates its code so
    # the report is scriptable.
    code = 0
    try:
        code = main()
    finally:
        sys.exit(code if "--doctor" in sys.argv[1:] else 0)
