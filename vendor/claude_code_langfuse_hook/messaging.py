"""Plain-English, throttled user-facing messages for the Stop hook.

Claude Code reads a Stop hook's stdout as JSON; a top-level ``systemMessage``
field is surfaced to the user. We use that to nudge the user when tracing is
enabled but can't run (e.g. missing `.env` keys) — without nagging on every
turn.

Throttling: one message per (reason, session) pair, recorded as a marker file
under ``~/.claude/state/warnings``. This mirrors the shell wrapper's throttle
so the Python and shell layers don't double-message for the same problem.

Everything here is best-effort and never raises: messaging must not be able to
break the fail-open hook.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

WARN_DIR = Path.home() / ".claude" / "state" / "warnings"
_MARKER_MAX_AGE_S = 14 * 24 * 3600  # prune markers older than 14 days


def _safe(s: str) -> str:
    return "".join(c if (c.isalnum() or c in "_.-") else "_" for c in s)


def warn_once(reason: str, session_id: str, message: str) -> None:
    """Print `message` as a Stop-hook systemMessage, at most once per session.

    Always emits to the log; emits to stdout (for the user) only the first
    time we see this (reason, session) pair.
    """
    log.info("remediation[%s]: %s", reason, message)
    try:
        WARN_DIR.mkdir(parents=True, exist_ok=True)
        _prune()
        marker = WARN_DIR / f"{_safe(reason)}_{_safe(session_id or 'global')}"
        if marker.exists():
            return  # already told them this session
        marker.touch(exist_ok=True)
    except OSError as exc:
        # If we can't manage the marker, fall through and still show the
        # message once — a duplicate nudge beats silent failure.
        log.warning("warn_once marker handling failed: %s", exc)

    try:
        sys.stdout.write(json.dumps({"systemMessage": message}) + "\n")
        sys.stdout.flush()
    except Exception as exc:  # pragma: no cover - stdout should always work
        log.warning("warn_once stdout write failed: %s", exc)


def _prune() -> None:
    now = time.time()
    try:
        for p in WARN_DIR.iterdir():
            try:
                if p.is_file() and now - p.stat().st_mtime > _MARKER_MAX_AGE_S:
                    p.unlink()
            except OSError:
                pass
    except OSError:
        pass
