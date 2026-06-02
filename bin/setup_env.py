#!/usr/bin/env python3
"""Idempotent upsert of the plugin's CC_* settings into a project's .env.

Used by the `/setup` slash command. Reads the values from its OWN environment
(the caller exports the CC_* vars it collected from the user) and writes them
into the target .env:

  - updates an existing `KEY=...` line in place (preserving position),
  - appends any new keys under a labelled block,
  - creates the file if it doesn't exist,
  - leaves every other line untouched,
  - writes atomically.

Target resolution (so we write the exact file the hook reads):
  argv[1] if given; otherwise the nearest existing `.env` walking up from the
  cwd, else `<nearest .git dir>/.env`, else `./.env`.

Secrets are never printed in full. Exits 0 on success, 1 if nothing to write.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Order in which managed keys are appended when newly added.
MANAGED = [
    "CC_TRACE_TO_LANGFUSE",
    "CC_PROJECT_NAME",
    "CC_LANGFUSE_BASE_URL",
    "CC_LANGFUSE_PUBLIC_KEY",
    "CC_LANGFUSE_SECRET_KEY",
]
OPTIONAL = [
    "CC_LANGFUSE_DEBUG",
    "CC_LANGFUSE_MAX_CHARS",
    "CC_LANGFUSE_TIMEOUT",
    "CC_LANGFUSE_FLUSH_TIMEOUT",
]
ALL_KEYS = MANAGED + OPTIONAL


def _mask(key: str, value: str) -> str:
    if "KEY" in key and len(value) > 8:
        return f"{value[:4]}…{value[-3:]}"
    return value


def _format_value(value: str) -> str:
    """Quote only when needed so the .env stays human-readable."""
    needs_quote = (
        value == ""
        or any(c.isspace() for c in value)
        or "#" in value
        or value[0] in ("'", '"')
    )
    if not needs_quote:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _key_of(line: str):
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        return None
    if s.startswith("export "):
        s = s[len("export "):].lstrip()
    return s.split("=", 1)[0].strip()


def _resolve_target(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    # Reuse the hook's own .env discovery so /setup writes the file the hook reads.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))
    try:
        from claude_code_langfuse_hook import config as cfg  # type: ignore

        found = cfg.find_env_file(Path.cwd())
        if found:
            return found
    except Exception:
        pass
    cur = Path.cwd().resolve()
    for d in [cur, *cur.parents]:
        if (d / ".git").exists():
            return d / ".env"
    return cur / ".env"


def main() -> int:
    # Accept values two ways so we don't depend on a specific shell:
    #   - exported env vars (CC_*=…) — used by the /setup command on Git Bash,
    #   - or trailing KEY=VALUE arguments — shell-agnostic (bash/cmd/PowerShell).
    # The first non-KEY=VALUE argument is the explicit target path.
    explicit: str | None = None
    cli_overrides: dict[str, str] = {}
    for arg in sys.argv[1:]:
        if "=" in arg:
            key, _, val = arg.partition("=")
            key = key.strip()
            if key in ALL_KEYS:
                cli_overrides[key] = val
        elif explicit is None and arg not in ("", "-"):
            explicit = arg

    target = _resolve_target(explicit)

    provided = {k: os.environ[k] for k in ALL_KEYS if os.environ.get(k)}
    provided.update(cli_overrides)  # explicit args win over the environment
    if not provided:
        print(
            "No CC_* values found in the environment — nothing to write. "
            "Export the values (e.g. CC_PROJECT_NAME=…) before calling this.",
            file=sys.stderr,
        )
        return 1

    existed = target.exists()
    lines = target.read_text(encoding="utf-8").splitlines() if existed else []

    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        k = _key_of(line)
        if k in provided:
            out.append(f"{k}={_format_value(provided[k])}")
            seen.add(k)
        else:
            out.append(line)

    new_keys = [k for k in ALL_KEYS if k in provided and k not in seen]
    if new_keys:
        if out and out[-1].strip() != "":
            out.append("")
        out.append("# --- Claude Code → Langfuse tracing (managed by /setup) ---")
        for k in new_keys:
            out.append(f"{k}={_format_value(provided[k])}")

    text = "\n".join(out).rstrip("\n") + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)

    verb = "Updated" if existed else "Created"
    print(f"{verb} {target.resolve()} with {len(provided)} variable(s):")
    for k in ALL_KEYS:
        if k in provided:
            tag = "updated" if k in seen else "added"
            print(f"  {k}={_mask(k, provided[k])}  ({tag})")

    _gitignore_hint(target)
    print("\nNext: run /claude-langfuse:test to confirm tracing works.")
    return 0


def _gitignore_hint(target: Path) -> None:
    """Warn (don't modify) if .env may not be gitignored in a git repo."""
    cur = target.resolve().parent
    for d in [cur, *cur.parents]:
        if (d / ".git").exists():
            gi = d / ".gitignore"
            ignored = False
            try:
                if gi.exists():
                    ignored = any(
                        line.strip().rstrip("/") in (".env", "*.env", target.name)
                        for line in gi.read_text(encoding="utf-8").splitlines()
                    )
            except OSError:
                pass
            if not ignored:
                print(
                    f"\n⚠ Heads up: {target.name} may not be gitignored in {d}. "
                    "Add it to .gitignore so your Langfuse keys aren't committed."
                )
            return


if __name__ == "__main__":
    sys.exit(main())
