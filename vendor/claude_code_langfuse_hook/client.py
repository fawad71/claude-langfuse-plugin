"""Langfuse client construction + a bounded, non-blocking drain.

Two concerns live here so the hook, the CLI `test`, and the `doctor` all
behave identically:

  build_client(cfg)
      Construct the v3 Langfuse client with an HTTP `timeout` so a single
      network call can never hang indefinitely.

  bounded_shutdown(client, timeout_s, log)
      Flush + shut the client down on a daemon thread and wait at most
      `timeout_s`. The Langfuse SDK already POSTs spans from its own
      background thread; all we do here is wait *briefly* for that buffer to
      drain before the short-lived hook process exits. If the drain exceeds
      the cap (slow / unreachable Langfuse) we return anyway — Claude Code is
      never stalled. The thread is a daemon, so it's torn down with the
      process; the only cost of a timeout is that a single trace may be
      dropped, which is the correct trade for a telemetry side-channel.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .config import Config


def build_client(cfg: Config) -> Any:
    """Construct a Langfuse v3 client bounded by `cfg.request_timeout`.

    Raises ImportError if the SDK isn't available so callers can fail open.
    """
    from langfuse import Langfuse

    return Langfuse(
        public_key=cfg.langfuse_public_key,
        secret_key=cfg.langfuse_secret_key,
        host=cfg.langfuse_base_url,
        # Per-HTTP-call cap. Without this the client's default (tens of
        # seconds) is the only bound on a hung connection.
        timeout=cfg.request_timeout,
    )


def bounded_shutdown(
    client: Any,
    timeout_s: float,
    log: logging.Logger | None = None,
) -> bool:
    """Flush + shut `client` down, waiting at most `timeout_s` seconds.

    Returns True if the drain completed within the cap, False if it timed out
    (and was left running on its daemon thread). Never raises.
    """
    log = log or logging.getLogger(__name__)

    def _drain() -> None:
        # shutdown() flushes internally, but calling flush() first makes the
        # export explicit and is harmless — the whole thing is time-capped by
        # the join() below regardless.
        try:
            client.flush()
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            client.shutdown()
        except Exception:  # pragma: no cover - defensive
            pass

    t = threading.Thread(target=_drain, name="langfuse-drain", daemon=True)
    start = time.time()
    t.start()
    t.join(timeout_s)
    elapsed = time.time() - start

    if t.is_alive():
        log.warning(
            "Langfuse flush/shutdown exceeded the %.1fs cap — returning so "
            "Claude Code isn't stalled. The send continues on a daemon thread "
            "and may be dropped when the process exits.",
            timeout_s,
        )
        return False

    log.debug("Langfuse flush/shutdown completed in %.2fs.", elapsed)
    return True
