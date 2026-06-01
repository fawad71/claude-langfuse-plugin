"""Langfuse dispatch — emit one trace per turn using the v3 SDK.

Schema we produce per turn:

  trace                (named "Claude Code - Turn N", session_id, user_id, tags)
    └─ root span       "Claude Code - Turn N"  (input = user text)
        ├─ generation  "Claude Response"        (model, input/output, token usage)
        ├─ tool span   "Tool: <name>"           (input + output, one per call)
        └─ span        "Sub-agent (Task)"       (nested sub-agent tool calls)

Each turn becomes its own Langfuse trace; the shared `session_id`
glues them together in the Sessions view. Large prompt / response /
tool-output bodies are truncated; metadata records the original length
and a sha256 so identity is recoverable.

Timing: when the Langfuse SDK exposes its OpenTelemetry internals (it does
on the pinned 3.x line) we **backdate** every observation to the real
timestamps recorded in the transcript, so durations and ordering in the
Langfuse UI reflect what actually happened rather than collapsing to the
moment the Stop hook fired. If those internals are ever absent we fall back
to live context-manager spans — less precise, but never broken.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .config import Config
from .transcript import (
    Turn,
    extract_text,
    extract_thinking,
    get_content,
    get_model,
    get_role,
    is_tool_result,
    iter_tool_results,
    iter_tool_uses,
    truncate_text,
    truncate_value,
)

log = logging.getLogger(__name__)

try:  # OpenTelemetry ships as a langfuse dependency; guard just in case.
    from opentelemetry import trace as _otel_trace_api
except Exception:  # pragma: no cover - defensive
    _otel_trace_api = None


# ---------------------------------------------------------------------------
# Timestamp helpers — transcript rows carry ISO-8601 timestamps we backdate to.
# ---------------------------------------------------------------------------
def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (tolerating a trailing 'Z'); None on failure."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _to_ns(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return int(dt.timestamp() * 1_000_000_000)


def _supports_backdating(langfuse: Any) -> bool:
    return (
        _otel_trace_api is not None
        and hasattr(langfuse, "_otel_tracer")
        and hasattr(langfuse, "_create_observation_from_otel_span")
    )


def _start_backdated(
    langfuse: Any,
    *,
    name: str,
    as_type: str,
    start_dt: Optional[datetime],
    parent_otel_span: Any = None,
    **obs_kwargs: Any,
) -> Any:
    """Create a Langfuse observation pinned to an explicit OTel start time.

    `start_span` accepts `start_time=None` (defaults to now), so a missing
    timestamp degrades to a live span rather than an error. The child is
    parented by activating `parent_otel_span` in the OTel context first.
    """
    start_ns = _to_ns(start_dt)
    tracer = langfuse._otel_tracer
    if parent_otel_span is not None:
        with _otel_trace_api.use_span(parent_otel_span, end_on_exit=False):
            otel_span = tracer.start_span(name=name, start_time=start_ns)
    else:
        otel_span = tracer.start_span(name=name, start_time=start_ns)
    return langfuse._create_observation_from_otel_span(
        otel_span=otel_span, as_type=as_type, **obs_kwargs
    )


# ---------------------------------------------------------------------------
# Tool call assembly — pulls tool_use blocks out of the assistant messages
# and pairs them with their tool_result counterparts by id.
# ---------------------------------------------------------------------------
def _build_tool_call(
    tu: dict,
    am_ts: Any,
    results_by_id: dict[str, Any],
    result_meta_by_id: dict[str, dict],
    max_chars: int,
) -> dict:
    """Assemble a single tool call (input/output + timing + error flag)."""
    tid = str(tu.get("id") or "")
    tool_input = tu.get("input")
    trunc_input, in_meta = truncate_value(tool_input, max_chars)

    raw_output = results_by_id.get(tid)
    if raw_output is None:
        trunc_output: Any = None
        out_meta = None
    elif isinstance(raw_output, str):
        trunc_output, out_meta = truncate_text(raw_output, max_chars)
        if not out_meta.get("truncated"):
            out_meta = None
    else:
        try:
            as_str = json.dumps(raw_output, ensure_ascii=False)
        except (TypeError, ValueError):
            as_str = str(raw_output)
        if len(as_str) <= max_chars:
            trunc_output, out_meta = raw_output, None
        else:
            trunc_output, out_meta = truncate_text(as_str, max_chars)

    meta = result_meta_by_id.get(tid) or {}
    return {
        "id": tid,
        "name": tu.get("name") or "unknown",
        "input": trunc_input,
        "input_meta": in_meta,
        "output": trunc_output,
        "output_meta": out_meta,
        "is_error": bool(meta.get("is_error")),
        "start_dt": _parse_ts(am_ts),
        "end_dt": _parse_ts(meta.get("timestamp")),
    }


def _tool_calls_for_turn(turn: Turn, max_chars: int) -> list[dict]:
    calls: list[dict] = []
    seen_ids: set[str] = set()
    for am in turn.assistant_msgs:
        am_ts = am.get("timestamp")
        for tu in iter_tool_uses(get_content(am)):
            tid = str(tu.get("id") or "")
            if tid and tid in seen_ids:
                continue
            if tid:
                seen_ids.add(tid)
            calls.append(
                _build_tool_call(
                    tu, am_ts, turn.tool_results_by_id, turn.tool_result_meta_by_id, max_chars
                )
            )
    return calls


def _subagent_tool_calls(subagent_msgs: list[dict], max_chars: int) -> list[dict]:
    """Tool calls made inside Task sub-agents (sidechain rows)."""
    results_by_id: dict[str, Any] = {}
    result_meta_by_id: dict[str, dict] = {}
    assistant_rows: list[dict] = []
    for m in subagent_msgs:
        if is_tool_result(m):
            row_ts = m.get("timestamp")
            for tr in iter_tool_results(get_content(m)):
                tid = str(tr.get("tool_use_id") or "")
                if tid:
                    results_by_id[tid] = tr.get("content")
                    result_meta_by_id[tid] = {
                        "is_error": bool(tr.get("is_error")),
                        "timestamp": row_ts,
                    }
        elif get_role(m) == "assistant":
            assistant_rows.append(m)

    calls: list[dict] = []
    seen: set[str] = set()
    for am in assistant_rows:
        am_ts = am.get("timestamp")
        for tu in iter_tool_uses(get_content(am)):
            tid = str(tu.get("id") or "")
            if tid and tid in seen:
                continue
            if tid:
                seen.add(tid)
            calls.append(
                _build_tool_call(tu, am_ts, results_by_id, result_meta_by_id, max_chars)
            )
    return calls


def _sum_usage(assistant_msgs: list[dict]) -> dict:
    """Sum Anthropic usage across every assistant call in a turn.

    Each assistant message in `turn.assistant_msgs` corresponds to one
    Anthropic API call inside the agent loop, and each carries its own
    `usage` block. We sum the four token categories so the generation's
    `usage_details` reflects the whole turn, not just the final step.
    """
    totals: dict[str, int] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    seen_ids: set[str] = set()
    for am in assistant_msgs:
        # Skip duplicate-id rows: streamed updates of the same message
        # carry cumulative usage on the latest row, so summing every
        # row with the same id would double-count.
        m = am.get("message") or {}
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
        u = m.get("usage") or {}
        for k in totals:
            v = u.get(k)
            try:
                totals[k] += int(v or 0)
            except (TypeError, ValueError):
                pass
    return totals


def _as_int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _usage_details(usage: dict) -> dict[str, int]:
    # Pass Anthropic's native usage field names verbatim so the keys match
    # Langfuse's default model-price catalog exactly (input_tokens,
    # output_tokens, cache_creation_input_tokens, cache_read_input_tokens).
    details: dict[str, int] = {
        "input_tokens": _as_int(usage.get("input_tokens")),
        "output_tokens": _as_int(usage.get("output_tokens")),
    }
    cache_creation = _as_int(usage.get("cache_creation_input_tokens"))
    cache_read = _as_int(usage.get("cache_read_input_tokens"))
    if cache_creation:
        details["cache_creation_input_tokens"] = cache_creation
    if cache_read:
        details["cache_read_input_tokens"] = cache_read
    return details


# ---------------------------------------------------------------------------
# Per-turn emission
# ---------------------------------------------------------------------------
def emit_turn(
    *,
    langfuse,
    cfg: Config,
    user_id: str,
    session_id: str,
    turn_num: int,
    turn: Turn,
    transcript_path: Path,
) -> None:
    """Emit one Langfuse trace for `turn`.

    The trace name encodes the turn number so each session reads as a
    natural sequence in the Langfuse UI.
    """
    from langfuse import propagate_attributes

    user_text_raw = extract_text(get_content(turn.user_msg))
    user_text, user_text_meta = truncate_text(user_text_raw, cfg.max_chars)

    last_assistant = turn.assistant_msgs[-1]
    assistant_text_raw = extract_text(get_content(last_assistant))
    assistant_text, assistant_text_meta = truncate_text(assistant_text_raw, cfg.max_chars)

    # Extended thinking — capture across all assistant messages in the turn
    # since long reasoning may be split across streamed message ids.
    thinking_raw = "\n".join(
        filter(None, (extract_thinking(get_content(am)) for am in turn.assistant_msgs))
    )
    thinking_text, thinking_text_meta = (
        truncate_text(thinking_raw, cfg.max_chars) if thinking_raw else ("", None)
    )

    model = get_model(turn.assistant_msgs[0])
    # A turn is an agent loop — each assistant message is its own Anthropic
    # API call with its own usage block. Sum across all of them so cache
    # reads / creations / input / output tokens from intermediate
    # tool-calling steps aren't dropped.
    usage = _sum_usage(turn.assistant_msgs)
    usage_details = _usage_details(usage)
    tool_calls = _tool_calls_for_turn(turn, cfg.max_chars)
    subagent_calls = _subagent_tool_calls(turn.subagent_msgs, cfg.max_chars)

    # ---- Timeline reconstruction from transcript timestamps --------------
    user_dt = _parse_ts(turn.user_msg.get("timestamp"))
    assistant_dts = [
        d for d in (_parse_ts(am.get("timestamp")) for am in turn.assistant_msgs) if d
    ]
    gen_start = assistant_dts[0] if assistant_dts else user_dt
    gen_end = assistant_dts[-1] if assistant_dts else gen_start
    turn_start = user_dt or gen_start
    end_candidates = [d for d in ([gen_end] + [tc["end_dt"] for tc in tool_calls]) if d]
    turn_end = max(end_candidates) if end_candidates else gen_end

    trace_name = f"Claude Code - Turn {turn_num}"
    # Build a composite `user_project:` tag (e.g.
    # "user_project:muhammad.fawad.ext-karzaty_api") so a single Langfuse
    # dashboard widget can group by user × project in one dimension.
    user_local = user_id.split("@", 1)[0] if "@" in user_id else user_id
    tags = [
        f"project:{cfg.project_name}",
        f"user_project:{user_local}-{cfg.project_name}",
        f"model:{model}",
        "claude-code",
    ]

    backdated = _supports_backdating(langfuse)
    trace_metadata = {
        "source": "claude-code",
        "project": cfg.project_name,
        "session_id": session_id,
        "turn_number": turn_num,
        "transcript_path": str(transcript_path),
        "user_text": user_text_meta,
        "tool_call_count": len(tool_calls),
        "subagent_tool_call_count": len(subagent_calls),
        "backdated": backdated,
    }
    generation_metadata: dict[str, Any] = {
        "assistant_text": assistant_text_meta,
        "stop_reason": (last_assistant.get("message") or {}).get("stop_reason"),
        "tool_count": len(tool_calls),
    }
    if thinking_raw:
        generation_metadata["thinking"] = thinking_text
        generation_metadata["thinking_meta"] = thinking_text_meta

    with propagate_attributes(
        session_id=session_id,
        user_id=user_id,
        trace_name=trace_name,
        tags=tags,
    ):
        if backdated:
            _emit_backdated(
                langfuse=langfuse,
                trace_name=trace_name,
                user_text=user_text,
                assistant_text=assistant_text,
                model=model,
                usage_details=usage_details,
                trace_metadata=trace_metadata,
                generation_metadata=generation_metadata,
                tool_calls=tool_calls,
                subagent_calls=subagent_calls,
                turn_start=turn_start,
                turn_end=turn_end,
                gen_start=gen_start,
                gen_end=gen_end,
            )
        else:
            _emit_live(
                langfuse=langfuse,
                trace_name=trace_name,
                user_text=user_text,
                assistant_text=assistant_text,
                model=model,
                usage_details=usage_details,
                trace_metadata=trace_metadata,
                generation_metadata=generation_metadata,
                tool_calls=tool_calls,
                gen_start=gen_start,
            )

    log.info(
        "Emitted turn user=%s project=%s session=%s turn=%d tools=%d subagent_tools=%d "
        "in=%d out=%d cache_create=%d cache_read=%d thinking=%d backdated=%s",
        user_id,
        cfg.project_name,
        session_id,
        turn_num,
        len(tool_calls),
        len(subagent_calls),
        usage_details.get("input_tokens", 0),
        usage_details.get("output_tokens", 0),
        usage_details.get("cache_creation_input_tokens", 0),
        usage_details.get("cache_read_input_tokens", 0),
        len(thinking_raw),
        backdated,
    )


def _tool_obs_kwargs(tc: dict) -> dict:
    kw: dict[str, Any] = {
        "input": tc["input"],
        "metadata": {
            "tool_name": tc["name"],
            "tool_id": tc["id"],
            "input_meta": tc["input_meta"],
            "output_meta": tc["output_meta"],
        },
    }
    if tc["is_error"]:
        kw["level"] = "ERROR"
        kw["status_message"] = "Tool call returned an error"
    return kw


def _emit_backdated(
    *,
    langfuse,
    trace_name: str,
    user_text: str,
    assistant_text: str,
    model: str,
    usage_details: dict,
    trace_metadata: dict,
    generation_metadata: dict,
    tool_calls: list[dict],
    subagent_calls: list[dict],
    turn_start: Optional[datetime],
    turn_end: Optional[datetime],
    gen_start: Optional[datetime],
    gen_end: Optional[datetime],
) -> None:
    """Emit the turn as backdated observations with real start/end times."""
    root = _start_backdated(
        langfuse,
        name=trace_name,
        as_type="span",
        start_dt=turn_start,
        input={"role": "user", "content": user_text},
        metadata=trace_metadata,
    )
    root_otel = root._otel_span
    try:
        gen = _start_backdated(
            langfuse,
            name="Claude Response",
            as_type="generation",
            start_dt=gen_start,
            parent_otel_span=root_otel,
            model=model,
            input={"role": "user", "content": user_text},
            output={"role": "assistant", "content": assistant_text},
            usage_details=usage_details,
            completion_start_time=gen_start,  # TTFT proxy
            metadata=generation_metadata,
        )
        gen.end(end_time=_to_ns(gen_end))

        for tc in tool_calls:
            tobs = _start_backdated(
                langfuse,
                name=f"Tool: {tc['name']}",
                as_type="tool",
                start_dt=tc["start_dt"] or gen_start,
                parent_otel_span=root_otel,
                **_tool_obs_kwargs(tc),
            )
            tobs.update(output=tc["output"])
            tobs.end(end_time=_to_ns(tc["end_dt"] or tc["start_dt"] or gen_end))

        if subagent_calls:
            sa_start = subagent_calls[0]["start_dt"] or gen_start
            sa_end_candidates = [c["end_dt"] for c in subagent_calls if c["end_dt"]]
            sa_end = max(sa_end_candidates) if sa_end_candidates else (gen_end or sa_start)
            sa_span = _start_backdated(
                langfuse,
                name="Sub-agent (Task)",
                as_type="agent",
                start_dt=sa_start,
                parent_otel_span=root_otel,
                metadata={"subagent_tool_call_count": len(subagent_calls)},
            )
            sa_otel = sa_span._otel_span
            try:
                for tc in subagent_calls:
                    tobs = _start_backdated(
                        langfuse,
                        name=f"Tool: {tc['name']}",
                        as_type="tool",
                        start_dt=tc["start_dt"] or sa_start,
                        parent_otel_span=sa_otel,
                        **_tool_obs_kwargs(tc),
                    )
                    tobs.update(output=tc["output"])
                    tobs.end(end_time=_to_ns(tc["end_dt"] or tc["start_dt"] or sa_end))
            finally:
                sa_span.end(end_time=_to_ns(sa_end))

        root.update(output={"role": "assistant", "content": assistant_text})
    finally:
        root.end(end_time=_to_ns(turn_end))


def _emit_live(
    *,
    langfuse,
    trace_name: str,
    user_text: str,
    assistant_text: str,
    model: str,
    usage_details: dict,
    trace_metadata: dict,
    generation_metadata: dict,
    tool_calls: list[dict],
    gen_start: Optional[datetime],
) -> None:
    """Fallback path: live context-manager spans (no backdating internals).

    Retains the original, well-tested emission shape; still applies the
    native enhancements (TTFT via completion_start_time, ERROR level on
    failed tool calls) that don't depend on the OTel internals.
    """
    with langfuse.start_as_current_observation(
        name=trace_name,
        input={"role": "user", "content": user_text},
        metadata=trace_metadata,
    ) as trace_span:
        with langfuse.start_as_current_observation(
            name="Claude Response",
            as_type="generation",
            model=model,
            input={"role": "user", "content": user_text},
            output={"role": "assistant", "content": assistant_text},
            usage_details=usage_details,
            completion_start_time=gen_start,
            metadata=generation_metadata,
        ):
            pass

        for tc in tool_calls:
            with langfuse.start_as_current_observation(
                name=f"Tool: {tc['name']}",
                as_type="tool",
                **_tool_obs_kwargs(tc),
            ) as tool_obs:
                tool_obs.update(output=tc["output"])

        trace_span.update(output={"role": "assistant", "content": assistant_text})
