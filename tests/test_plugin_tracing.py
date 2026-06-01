"""Tests for the plugin-specific tracing enhancements.

Run from the plugin root with the vendored package on the path:

    PYTHONPATH=vendor python -m pytest tests/ -q

These cover the four enhancements layered on top of the vendored package:
backdated timestamps, ERROR level on failed tools, TTFT (completion_start_time),
and Task sub-agent (sidechain) nesting — plus the sidechain turn-boundary fix.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from claude_code_langfuse_hook import tracer as TR
from claude_code_langfuse_hook import transcript as T
from claude_code_langfuse_hook.config import Config


def _iso_ns(s: str) -> int:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return int(datetime.fromisoformat(s).timestamp() * 1_000_000_000)


def _cfg() -> Config:
    return Config(
        project_root=Path("."),
        env_path=None,
        trace_enabled=True,
        project_name="demo",
        langfuse_base_url="http://127.0.0.1:1",
        langfuse_public_key="pk",
        langfuse_secret_key="sk",
        debug=False,
        max_chars=20_000,
    )


@pytest.fixture
def rich_turn() -> T.Turn:
    msgs = [
        {"type": "user", "timestamp": "2026-06-01T10:00:00.000Z",
         "message": {"role": "user", "content": "go"}},
        {"type": "assistant", "timestamp": "2026-06-01T10:00:02.000Z",
         "message": {"id": "a1", "role": "assistant", "model": "claude-opus-4",
                     "stop_reason": "tool_use",
                     "usage": {"input_tokens": 10, "output_tokens": 5},
                     "content": [
                         {"type": "tool_use", "id": "tu_bash", "name": "Bash", "input": {"cmd": "false"}},
                         {"type": "tool_use", "id": "tu_task", "name": "Task", "input": {"d": "sub"}}]}},
        {"type": "user", "timestamp": "2026-06-01T10:00:03.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu_bash", "is_error": True, "content": "boom"}]}},
        # sidechain (Task sub-agent)
        {"type": "assistant", "isSidechain": True, "timestamp": "2026-06-01T10:00:04.000Z",
         "message": {"id": "s1", "role": "assistant", "content": [
             {"type": "tool_use", "id": "tu_grep", "name": "Grep", "input": {"q": "x"}}]}},
        {"type": "user", "isSidechain": True, "timestamp": "2026-06-01T10:00:05.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu_grep", "content": "hits"}]}},
        {"type": "assistant", "timestamp": "2026-06-01T10:00:06.000Z",
         "message": {"id": "a2", "role": "assistant", "stop_reason": "end_turn",
                     "usage": {"input_tokens": 5, "output_tokens": 7},
                     "content": [{"type": "text", "text": "done"}]}},
    ]
    turns = T.build_turns(msgs)
    assert len(turns) == 1, "sidechain rows must not open a top-level turn"
    return turns[0]


# ---------------------------------------------------------------------------
# Transcript-level (pure-python) behaviour
# ---------------------------------------------------------------------------
def test_sidechain_does_not_split_turns(rich_turn: T.Turn) -> None:
    assert len(rich_turn.subagent_msgs) == 2  # one asst + one tool_result row


def test_tool_result_meta_captures_error_and_timestamp(rich_turn: T.Turn) -> None:
    meta = rich_turn.tool_result_meta_by_id["tu_bash"]
    assert meta["is_error"] is True
    assert meta["timestamp"] == "2026-06-01T10:00:03.000Z"


def test_parse_ts_tolerates_z_and_offset() -> None:
    assert TR._parse_ts("2026-06-01T10:00:00Z") is not None
    assert TR._parse_ts("2026-06-01T10:00:00+00:00") is not None
    assert TR._parse_ts("not-a-date") is None
    assert TR._parse_ts(None) is None


# ---------------------------------------------------------------------------
# Tracer-level behaviour against the real SDK internals
# ---------------------------------------------------------------------------
def _record_emit(turn: T.Turn):
    """Run emit_turn against a real Langfuse client, recording observations."""
    langfuse = pytest.importorskip("langfuse")
    from langfuse import Langfuse

    lf = Langfuse(public_key="pk-lf-x", secret_key="sk-lf-x",
                  host="http://127.0.0.1:1", tracing_enabled=True)
    assert TR._supports_backdating(lf), "expected OTel backdating internals on pinned SDK"

    created: list[dict] = []
    real = lf._create_observation_from_otel_span

    def rec(*, otel_span, as_type, **kw):
        created.append({"as_type": as_type, "name": getattr(otel_span, "name", None), **kw})
        return real(otel_span=otel_span, as_type=as_type, **kw)

    lf._create_observation_from_otel_span = rec

    starts: dict[str, int] = {}
    ends: dict[str, int] = {}
    real_tracer = lf._otel_tracer

    class RecTracer:
        def start_span(self, name, start_time=None, **kw):
            span = real_tracer.start_span(name, start_time=start_time, **kw)
            starts[name] = start_time
            real_end = span.end

            def end(end_time=None):
                ends[name] = end_time
                return real_end(end_time=end_time)

            span.end = end
            return span

    lf._otel_tracer = RecTracer()

    TR.emit_turn(langfuse=lf, cfg=_cfg(), user_id="u@x.com", session_id="s",
                 turn_num=1, turn=turn, transcript_path=Path("/tmp/t"))
    return created, starts, ends


def test_failed_tool_gets_error_level(rich_turn: T.Turn) -> None:
    created, _, _ = _record_emit(rich_turn)
    bash = next(c for c in created if c["name"] == "Tool: Bash")
    assert bash.get("level") == "ERROR"
    ok = next(c for c in created if c["name"] == "Tool: Task")
    assert ok.get("level") is None


def test_subagent_nested_under_agent_span(rich_turn: T.Turn) -> None:
    created, _, _ = _record_emit(rich_turn)
    assert any(c["name"] == "Sub-agent (Task)" and c["as_type"] == "agent" for c in created)
    assert any(c["name"] == "Tool: Grep" for c in created)


def test_generation_has_ttft(rich_turn: T.Turn) -> None:
    created, _, _ = _record_emit(rich_turn)
    gen = next(c for c in created if c["name"] == "Claude Response")
    assert gen.get("completion_start_time") is not None


def test_backdated_start_and_end_times(rich_turn: T.Turn) -> None:
    _, starts, ends = _record_emit(rich_turn)
    assert starts["Claude Code - Turn 1"] == _iso_ns("2026-06-01T10:00:00.000Z")
    assert ends["Claude Code - Turn 1"] == _iso_ns("2026-06-01T10:00:06.000Z")
    assert starts["Claude Response"] == _iso_ns("2026-06-01T10:00:02.000Z")
    assert ends["Tool: Bash"] == _iso_ns("2026-06-01T10:00:03.000Z")
