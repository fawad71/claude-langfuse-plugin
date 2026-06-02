"""`--doctor` — a self-contained health report for the plugin.

Runs end to end via the same `uv run … bin/run_hook.py --doctor` invocation
the hooks use, so it exercises the *real* runtime (uv-provisioned Python + the
langfuse SDK) rather than whatever Python happens to be on PATH. Surfaced to
users through the `/langfuse-doctor` slash command.

It answers the three questions a user actually has:
  1. Are the system requirements met?  (Python, the langfuse SDK)
  2. Is my config correct?            (.env discovery, required CC_* vars)
  3. Does it actually reach Langfuse?  (a live, time-capped synthetic trace)

Prints a plain checklist with ✓ / ✗ / – markers and a final verdict. Returns
0 when tracing is fully ready, 1 otherwise — but never raises.
"""

from __future__ import annotations

import os
import platform
import sys
import time
from pathlib import Path

from . import __version__, client as client_mod, config as config_mod, identity

OK = "✓"      # ✓
BAD = "✗"     # ✗
SKIP = "–"    # –


def _mask(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-3:]}"


def _project_dir() -> Path:
    env_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if env_dir:
        return Path(env_dir)
    return Path.cwd()


def run_doctor() -> int:
    print(f"claude-langfuse doctor (v{__version__})\n")

    # ---- 1. System requirements ------------------------------------------
    print("System:")
    print(f"  {OK} Python {platform.python_version()} ({platform.system()} {platform.machine()})")
    print(f"  {OK} runtime path: {sys.executable}")

    sdk_ok = False
    try:
        from importlib.metadata import version as _pkg_version

        from langfuse import Langfuse  # noqa: F401

        print(f"  {OK} langfuse SDK {_pkg_version('langfuse')} importable")
        sdk_ok = True
    except Exception as exc:
        print(f"  {BAD} langfuse SDK not importable: {exc}")

    # ---- 2. Config -------------------------------------------------------
    project_dir = _project_dir()
    cfg = config_mod.resolve(project_dir)
    print("\nConfig:")
    print(f"  working dir:   {project_dir}")
    if cfg.env_path:
        print(f"  {OK} .env found:  {cfg.env_path}")
    else:
        print(f"  {SKIP} .env file:   none found (values must come from the OS env)")
    print(f"  project_name:  {cfg.project_name}")
    print(f"  trace_enabled: {cfg.trace_enabled}")
    print(f"  base_url:      {cfg.langfuse_base_url or '(empty)'}")
    print(f"  public_key:    {_mask(cfg.langfuse_public_key)}")
    print(f"  secret_key:    {_mask(cfg.langfuse_secret_key)}")
    print(f"  request_timeout/flush_timeout: {cfg.request_timeout}s / {cfg.flush_timeout}s")

    if not cfg.trace_enabled:
        print(f"  {BAD} CC_TRACE_TO_LANGFUSE is not true — tracing is OFF for this project.")
    if cfg.trace_enabled and not cfg.is_complete:
        print(f"  {BAD} Missing required vars: {', '.join(cfg.missing_fields())}")

    # ---- 3. Identity -----------------------------------------------------
    user_id = identity.resolve_user_id(cfg.project_root)
    print(f"\nIdentity:\n  user_id: {user_id}  (from git config user.email, or OS user)")

    # ---- 4. Live connectivity -------------------------------------------
    print("\nConnectivity:")
    conn_ok = False
    if not sdk_ok:
        print(f"  {SKIP} skipped — langfuse SDK unavailable.")
    elif not cfg.is_complete:
        print(f"  {SKIP} skipped — config incomplete; fix the vars above first.")
    else:
        conn_ok = _connectivity_check(cfg, user_id)

    # ---- Verdict ---------------------------------------------------------
    ready = sdk_ok and cfg.trace_enabled and cfg.is_complete and conn_ok
    print("\n" + ("=" * 56))
    if ready:
        print(f"{OK} READY — tracing is configured and Langfuse is reachable.")
        print(f"  Traces appear in Langfuse under Sessions, tagged project:{cfg.project_name}.")
        return 0

    print(f"{BAD} NOT READY — fix the items marked {BAD} above.")
    if not sdk_ok:
        print("  • The langfuse SDK failed to load (re-run; uv provisions it on first use).")
    if not cfg.trace_enabled:
        print("  • Set CC_TRACE_TO_LANGFUSE=true in your project's .env.")
    elif not cfg.is_complete:
        print(f"  • Add the missing keys to {cfg.env_path or 'your .env'}: "
              f"{', '.join(cfg.missing_fields())}")
    elif not conn_ok:
        print(f"  • The keys are set but Langfuse at {cfg.langfuse_base_url} didn't accept "
              "the test trace — check the URL, keys, and network/VPN.")
    return 1


def _connectivity_check(cfg: config_mod.Config, user_id: str) -> bool:
    """Send a synthetic trace with a time-capped drain. Returns success."""
    try:
        from langfuse import propagate_attributes
    except Exception as exc:
        print(f"  {BAD} could not import langfuse helpers: {exc}")
        return False

    try:
        client = client_mod.build_client(cfg)
    except Exception as exc:
        print(f"  {BAD} client init failed: {exc}")
        return False

    trace_name = f"claude-langfuse:doctor:{cfg.project_name}"
    start = time.time()
    try:
        with propagate_attributes(
            session_id="claude-langfuse-doctor",
            user_id=user_id,
            trace_name=trace_name,
            tags=[f"project:{cfg.project_name}", "synthetic", "claude-code", "doctor"],
        ):
            with client.start_as_current_observation(
                name=trace_name,
                input={"role": "user", "content": "ping"},
                metadata={"source": "claude-langfuse doctor"},
            ) as span:
                span.update(output={"role": "assistant", "content": "pong"})
    except Exception as exc:
        print(f"  {BAD} emitting the test trace failed: {exc}")
        return False

    drained = client_mod.bounded_shutdown(client, cfg.flush_timeout)
    elapsed = time.time() - start
    if drained:
        print(f"  {OK} sent a synthetic trace to {cfg.langfuse_base_url} in {elapsed:.2f}s")
        print(f"     look for trace '{trace_name}' (session: claude-langfuse-doctor)")
        return True

    print(f"  {BAD} the send didn't complete within {cfg.flush_timeout}s "
          f"(host slow or unreachable: {cfg.langfuse_base_url})")
    return False
