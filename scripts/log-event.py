#!/usr/bin/env python3
"""Pydantic-AI compatible OTel trace exporter for Claude Code.

Mode 1 (LOGFIRE_LOCAL_LOG set): Append JSONL to local log file
Mode 2 (LOGFIRE_TOKEN set): Send OTel spans to Logfire via OTLP/HTTP JSON

Trace hierarchy (pydantic-ai style):
  Claude Code session (root span)
  +-- chat claude-opus-4-6       <- LLM API call 1
  +-- chat claude-opus-4-6       <- LLM API call 2
  ...
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

VERSION = "0.4.0"

OTLP_EVENTS = {"SessionStart", "Stop", "SubagentStop", "SessionEnd"}

MODEL_PRICING: dict[str, tuple[float, float]] = {
    "opus": (0.000015, 0.000075),
    "sonnet": (0.000003, 0.000015),
    "haiku": (0.0000008, 0.000004),
}

TOOL_CATEGORIES: dict[str, str] = {
    "Read": "file_ops",
    "Write": "file_ops",
    "Edit": "file_ops",
    "MultiEdit": "file_ops",
    "NotebookRead": "file_ops",
    "NotebookEdit": "file_ops",
    "Glob": "search",
    "Grep": "search",
    "LS": "search",
    "ToolSearch": "search",
    "Bash": "execution",
    "WebSearch": "web",
    "WebFetch": "web",
    "Task": "agent",
    "Skill": "skill",
    "TodoRead": "planning",
    "TodoWrite": "planning",
    "TaskCreate": "planning",
    "TaskUpdate": "planning",
    "TaskList": "planning",
    "TaskGet": "planning",
    "EnterPlanMode": "planning",
    "ExitPlanMode": "planning",
    "AskUserQuestion": "interaction",
}


def categorize_tool(name: str) -> str:
    if name in TOOL_CATEGORIES:
        return TOOL_CATEGORIES[name]
    if name.startswith("mcp__"):
        return "mcp"
    return "other"


def format_tool_name(name: str) -> str:
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return f"{parts[1]}/{parts[2]}"
    return name


# ---------------------------------------------------------------------------
# Globals set once in main()
# ---------------------------------------------------------------------------
_hook_event = "unknown"
_session_id = "unknown"
_diag_log: str | None = None


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def log_diag(level: str, msg: str, detail: str | None = None) -> None:
    if not _diag_log:
        return
    entry: dict = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "level": level,
        "hook_event": _hook_event,
        "session_id": _session_id,
        "message": msg,
    }
    if detail:
        entry["detail"] = detail
    try:
        with open(_diag_log, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def now_nano() -> int:
    return time.time_ns()


def random_span_id() -> str:
    return os.urandom(8).hex()


def trace_id_from_session(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def iso_to_nano(iso_str: str) -> int | None:
    try:
        ts = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(ts.timestamp() * 1e9)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# OTLP construction
# ---------------------------------------------------------------------------


def to_otlp_anyvalue(val: object) -> dict:
    """Recursively convert a Python value to OTLP AnyValue."""
    if isinstance(val, bool):
        return {"boolValue": val}
    if isinstance(val, int):
        return {"intValue": str(val)}
    if isinstance(val, float):
        return {"doubleValue": val}
    if val is None:
        return {"stringValue": ""}
    if isinstance(val, str):
        return {"stringValue": val}
    if isinstance(val, list):
        return {"arrayValue": {"values": [to_otlp_anyvalue(v) for v in val]}}
    if isinstance(val, dict):
        return {"kvlistValue": {"values": [{"key": k, "value": to_otlp_anyvalue(v)} for k, v in val.items()]}}
    return {"stringValue": str(val)}


def make_attr(key: str, val: str) -> dict:
    return {"key": key, "value": {"stringValue": val}}


def make_int_attr(key: str, val: int) -> dict:
    return {"key": key, "value": {"intValue": str(val)}}


def make_double_attr(key: str, val: float) -> dict:
    return {"key": key, "value": {"doubleValue": val}}


def make_complex_attr(key: str, val: object) -> dict:
    return {"key": key, "value": to_otlp_anyvalue(val)}


def build_span(
    trace_id: str,
    span_id: str,
    parent_span_id: str,
    name: str,
    start_ns: int,
    end_ns: int,
    attrs: list[dict],
) -> dict:
    span: dict = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": attrs,
        "status": {"code": 1},
    }
    if parent_span_id:
        span["parentSpanId"] = parent_span_id
    return span


def build_otlp_envelope(spans: list[dict]) -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        make_attr("service.name", "claude-code-plugin"),
                        make_attr("service.version", VERSION),
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "claude-code-logfire",
                            "version": VERSION,
                        },
                        "spans": spans,
                    }
                ],
            }
        ]
    }


def send_otlp(payload: dict, endpoint: str, token: str) -> None:
    data = json.dumps(payload).encode()
    req = Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": token,
            "User-Agent": f"logfire/{VERSION}",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=5):
            pass
    except HTTPError as e:
        log_diag("warn", "OTLP export failed", f"http_status={e.code}")
        print(f"[logfire-plugin] OTLP export failed (HTTP {e.code})", file=sys.stderr)
    except (URLError, OSError):
        log_diag("warn", "curl failed (network/timeout)")
        print("[logfire-plugin] OTLP export failed (network/timeout)", file=sys.stderr)


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------


def get_model_prices(model: str) -> tuple[float, float] | None:
    for key, prices in MODEL_PRICING.items():
        if key in model:
            return prices
    return None


def calculate_cost(
    model: str,
    raw_input: int,
    output_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> float | None:
    prices = get_model_prices(model)
    if not prices:
        return None
    ip, op = prices
    return (raw_input * ip) + (cache_creation * ip * 1.25) + (cache_read * ip * 0.1) + (output_tokens * op)


def build_cost_details(
    model: str,
    raw_input: int,
    output_tokens: int,
    cache_creation: int = 0,
    cache_read: int = 0,
) -> list[dict] | None:
    prices = get_model_prices(model)
    if not prices:
        return None
    ip, op = prices
    input_cost = (raw_input * ip) + (cache_creation * ip * 1.25) + (cache_read * ip * 0.1)
    output_cost = output_tokens * op
    base_attrs = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "anthropic",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.system": "anthropic",
    }
    return [
        {"attributes": {**base_attrs, "gen_ai.token.type": "input"}, "total": input_cost},
        {"attributes": {**base_attrs, "gen_ai.token.type": "output"}, "total": output_cost},
    ]


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------


def read_state(state_file: str) -> dict | None:
    try:
        with open(state_file) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_state(state_file: str, state: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(state_file) + ".", dir=os.path.dirname(state_file))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, state_file)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def acquire_lock(lock_dir: str) -> bool:
    for _ in range(50):
        try:
            os.mkdir(lock_dir)
            return True
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(lock_dir)
                if age > 30:
                    try:
                        os.rmdir(lock_dir)
                    except OSError:
                        pass
                    continue
            except OSError:
                pass
            time.sleep(0.1)
    log_diag("warn", "Could not acquire session lock after 5s")
    return False


def release_lock(lock_dir: str) -> None:
    try:
        os.rmdir(lock_dir)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------


def _infer_finish_reason(assistant_msg: dict) -> str:
    stop_reason = assistant_msg.get("message", {}).get("stop_reason")
    if stop_reason == "end_turn":
        return "stop"
    if stop_reason == "tool_use":
        return "tool_call"
    if stop_reason is not None:
        return stop_reason
    content = assistant_msg.get("message", {}).get("content", [])
    if any(c.get("type") == "tool_use" for c in content if isinstance(c, dict)):
        return "tool_call"
    return "stop"


def _convert_input_message(line: dict) -> dict:
    """Convert a transcript user line to pydantic-ai input message format."""
    content = line.get("message", {}).get("content", "")
    parts = []
    if isinstance(content, str):
        parts.append({"type": "text", "content": content})
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                parts.append({"type": "text", "content": str(block)})
                continue
            if block.get("type") == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, str):
                    try:
                        result_content = json.loads(result_content)
                    except (json.JSONDecodeError, TypeError):
                        pass
                parts.append(
                    {
                        "type": "tool_call_response",
                        "id": block.get("tool_use_id"),
                        "name": block.get("name"),
                        "result": result_content,
                    }
                )
            elif block.get("type") == "text":
                parts.append({"type": "text", "content": block.get("text") or block.get("content", "")})
            else:
                parts.append({"type": "text", "content": json.dumps(block)})
    else:
        parts.append({"type": "text", "content": str(content)})
    return {"role": "user", "parts": parts}


def _convert_output_message(line: dict) -> dict:
    """Convert a transcript assistant line to pydantic-ai output message format."""
    content = line.get("message", {}).get("content", [])
    finish_reason = _infer_finish_reason(line)
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "content": block.get("text", "")})
        elif btype == "thinking":
            parts.append({"type": "thinking", "thinking": block.get("thinking", "")})
        elif btype == "tool_use":
            parts.append(
                {
                    "type": "tool_call",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "arguments": json.dumps(block.get("input", {})),
                }
            )
    return {"role": "assistant", "parts": parts, "finish_reason": finish_reason}


def _merge_assistant_content(base: dict, new: dict) -> None:
    """Merge a streaming assistant fragment into the base message.

    Claude Code writes each new content block (text, thinking, tool_use) as a
    separate transcript line sharing the same message.id.  We accumulate all
    blocks into the base message's content array, deduplicating tool_use blocks
    by their unique ``id`` field.  Text and thinking blocks are replaced by the
    latest version (the last fragment is the most complete for those types).
    """
    base_msg = base.setdefault("message", {})
    base_content = base_msg.setdefault("content", [])
    new_content = new.get("message", {}).get("content", [])

    seen_tool_ids = {b["id"] for b in base_content if isinstance(b, dict) and b.get("type") == "tool_use" and "id" in b}

    for block in new_content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            if block.get("id") not in seen_tool_ids:
                base_content.append(block)
                seen_tool_ids.add(block["id"])
        elif btype == "thinking":
            for i, b in enumerate(base_content):
                if isinstance(b, dict) and b.get("type") == "thinking":
                    base_content[i] = block
                    break
            else:
                base_content.append(block)
        elif btype == "text":
            for i in range(len(base_content) - 1, -1, -1):
                if isinstance(base_content[i], dict) and base_content[i].get("type") == "text":
                    base_content[i] = block
                    break
            else:
                base_content.append(block)
        else:
            base_content.append(block)

    new_msg = new.get("message", {})
    if new_msg.get("stop_reason") is not None:
        base_msg["stop_reason"] = new_msg["stop_reason"]
    if new_msg.get("usage"):
        base_msg["usage"] = new_msg["usage"]
    if new.get("timestamp"):
        base["timestamp"] = new["timestamp"]


def parse_transcript_slice(transcript_path: str | None, last_line: int) -> tuple[list[dict], int]:
    """Parse new transcript lines, returning (api_calls, new_total_lines).

    Each api_call has: model, timestamp, stop_reason, usage,
    input_messages, output_messages.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return [], last_line

    max_attempts = int(os.environ.get("TRANSCRIPT_READ_RETRIES", "20"))
    retry_delay = float(os.environ.get("TRANSCRIPT_READ_DELAY_SECONDS", "0.1"))

    for attempt in range(max_attempts + 1):
        try:
            with open(transcript_path) as f:
                all_lines_raw = f.readlines()
        except OSError:
            return [], last_line

        total_lines = len(all_lines_raw)
        if total_lines <= last_line:
            if attempt < max_attempts:
                time.sleep(retry_delay)
                continue
            return [], last_line

        new_lines_raw = all_lines_raw[last_line:]
        parsed = []
        for raw in new_lines_raw:
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed.append(json.loads(raw))
            except json.JSONDecodeError:
                continue

        # Keep only user and assistant lines
        relevant = [line for line in parsed if line.get("type") in ("user", "assistant")]

        # Merge assistant streaming fragments by message.id.
        # Claude Code writes each content block as a separate fragment,
        # so we must collect all blocks to reconstruct the full message.
        seen: dict[str, dict] = {}
        order: list[str] = []
        for line in relevant:
            if line.get("type") == "assistant":
                key = line.get("message", {}).get("id") or line.get("uuid", id(line))
            else:
                key = line.get("uuid", id(line))
            key = str(key)
            if key not in seen:
                order.append(key)
                seen[key] = line
            elif line.get("type") == "assistant":
                _merge_assistant_content(seen[key], line)
            else:
                seen[key] = line
        deduped = [seen[k] for k in order]

        # Group into API calls: each assistant message defines a call boundary,
        # preceded by any user messages since the last assistant.
        assistants = [line for line in deduped if line.get("type") == "assistant"]
        if not assistants:
            if attempt < max_attempts:
                time.sleep(retry_delay)
                continue
            return [], total_lines

        api_calls = []
        current_users: list[dict] = []
        prev_assistant_msg: dict | None = None
        for entry in deduped:
            if entry.get("type") == "user":
                current_users.append(entry)
            elif entry.get("type") == "assistant":
                asst = entry
                call_model = asst.get("message", {}).get("model", "")
                usage = asst.get("message", {}).get("usage", {})
                input_msgs = []
                if prev_assistant_msg is not None:
                    input_msgs.append(_convert_output_message(prev_assistant_msg))
                input_msgs.extend(_convert_input_message(u) for u in current_users)
                api_calls.append(
                    {
                        "model": call_model,
                        "timestamp": asst.get("timestamp", ""),
                        "stop_reason": _infer_finish_reason(asst),
                        "usage": usage,
                        "input_messages": input_msgs,
                        "output_messages": [_convert_output_message(asst)],
                    }
                )
                prev_assistant_msg = asst
                current_users = []

        if not api_calls and attempt < max_attempts:
            time.sleep(retry_delay)
            continue

        return api_calls, total_lines

    return [], last_line


# ---------------------------------------------------------------------------
# Shared span builder (eliminates Stop/SessionEnd duplication)
# ---------------------------------------------------------------------------


def _extract_tools_from_messages(messages: list[dict]) -> tuple[list[str], str | None, list[str], dict[str, int]]:
    """Extract tool names, skill name, and categories from message parts.

    Returns (display_tool_names, skill_name_or_none, unique_categories, tool_counts).
    tool_counts maps display name -> total invocation count (not deduplicated).
    """
    raw_names: list[str] = []
    tool_counts: dict[str, int] = {}
    skill_name: str | None = None

    for msg in messages:
        for part in msg.get("parts", []):
            if part.get("type") != "tool_call":
                continue
            name = part.get("name", "")
            if not name:
                continue

            display = format_tool_name(name)
            tool_counts[display] = tool_counts.get(display, 0) + 1

            if name in raw_names:
                continue
            raw_names.append(name)

            if name == "Skill":
                args_str = part.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    skill_name = args.get("skill")
                except (json.JSONDecodeError, AttributeError):
                    pass

    display_names = [format_tool_name(n) for n in raw_names]
    categories = list(dict.fromkeys(categorize_tool(n) for n in raw_names))
    return display_names, skill_name, categories, tool_counts


def _has_thinking(messages: list[dict]) -> bool:
    """Check if any message contains a thinking block."""
    return any(
        p.get("type") == "thinking"
        for msg in messages
        for p in msg.get("parts", [])
    )


def _extract_user_snippet(input_messages: list[dict], max_len: int = 60) -> str | None:
    """Extract a short snippet of the user's question from input messages."""
    for msg in input_messages:
        if msg.get("role") != "user":
            continue
        for part in msg.get("parts", []):
            if part.get("type") == "text":
                text = (part.get("content") or part.get("text") or "").strip()
                if not text:
                    continue
                first_line = text.split("\n", 1)[0].strip()
                if len(first_line) > max_len:
                    return first_line[:max_len] + "..."
                return first_line
    return None


def _describe_call(input_messages: list[dict], output_messages: list[dict]) -> str:
    """Derive a descriptive label from a call's input and output messages."""
    out_tools, skill_name, _, _ = _extract_tools_from_messages(output_messages)
    in_tools, _, _, _ = _extract_tools_from_messages(input_messages)
    thinking = _has_thinking(output_messages)

    if skill_name:
        other_tools = [t for t in out_tools if t != "Skill"]
        if other_tools:
            return f"Skill: {skill_name} (+{len(other_tools)} tools)"
        return f"Skill: {skill_name}"

    user_snippet = _extract_user_snippet(input_messages)
    has_tool_results = any(
        p.get("type") == "tool_call_response"
        for msg in input_messages
        for p in msg.get("parts", [])
    )

    thinking_prefix = "Thinking + " if thinking else ""

    if user_snippet:
        if out_tools:
            tool_summary = ", ".join(out_tools[:3])
            if len(out_tools) > 3:
                tool_summary += f" +{len(out_tools) - 3} more"
            return f"User: {user_snippet} -> {thinking_prefix}{tool_summary}"
        return f"User: {user_snippet} -> {thinking_prefix}Response"

    if out_tools:
        tool_summary = ", ".join(out_tools[:3])
        if len(out_tools) > 3:
            tool_summary += f" +{len(out_tools) - 3} more"
        if has_tool_results:
            return f"{thinking_prefix}{tool_summary} (processing tool results)"
        return f"{thinking_prefix}{tool_summary}"

    if has_tool_results:
        if in_tools:
            tool_summary = ", ".join(in_tools[:3])
            if len(in_tools) > 3:
                tool_summary += f" +{len(in_tools) - 3} more"
            return f"{thinking_prefix}Response (after {tool_summary})"
        return f"{thinking_prefix}Response (after tools)"
    return f"{thinking_prefix}Response" if thinking else "Response"


def build_child_spans_from_calls(
    api_calls: list[dict],
    trace_id: str,
    root_span_id: str,
    model_default: str,
    ts_nano: int,
    subagent_type: str = "",
) -> tuple[list[dict], list[dict], list[dict], dict, dict]:
    """Build child spans and accumulate state from API calls.

    Returns (spans, new_messages, new_cost_details, usage_deltas, tools_meta).
    """
    spans = []
    new_messages: list[dict] = []
    new_cost_details: list[dict] = []
    usage_deltas = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    tools_meta: dict = {"tools_used": {}, "categories": [], "skills": []}

    for call in api_calls:
        call_model = call.get("model") or model_default
        call_stop_reason = call.get("stop_reason", "stop")
        call_timestamp = call.get("timestamp", "")

        usage = call.get("usage", {})
        raw_input = usage.get("input_tokens", 0) or 0
        output_tokens = usage.get("output_tokens", 0) or 0
        cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
        cache_read = usage.get("cache_read_input_tokens", 0) or 0
        input_tokens = raw_input + cache_creation + cache_read

        usage_deltas["input_tokens"] += input_tokens
        usage_deltas["output_tokens"] += output_tokens
        usage_deltas["cache_creation_input_tokens"] += cache_creation
        usage_deltas["cache_read_input_tokens"] += cache_read

        cost = calculate_cost(call_model, raw_input, output_tokens, cache_creation, cache_read)
        cost_detail = build_cost_details(call_model, raw_input, output_tokens, cache_creation, cache_read)
        if cost_detail:
            new_cost_details.extend(cost_detail)

        call_input_msgs = call.get("input_messages", [])
        call_output_msgs = call.get("output_messages", [])
        for msg in call_input_msgs:
            if msg.get("role") == "user":
                new_messages.append(msg)
        new_messages.extend(call_output_msgs)

        # Span timing
        call_ns = ts_nano
        if call_timestamp:
            parsed_ns = iso_to_nano(call_timestamp)
            if parsed_ns:
                call_ns = parsed_ns

        span_id = random_span_id()
        out_tools, skill_name, categories, tool_counts = _extract_tools_from_messages(call_output_msgs)

        # Accumulate tools metadata across all calls (using raw counts)
        for tool_name, count in tool_counts.items():
            tools_meta["tools_used"][tool_name] = tools_meta["tools_used"].get(tool_name, 0) + count
        for cat in categories:
            if cat not in tools_meta["categories"]:
                tools_meta["categories"].append(cat)
        if skill_name and skill_name not in tools_meta["skills"]:
            tools_meta["skills"].append(skill_name)

        if subagent_type:
            snippet = _extract_user_snippet(call_input_msgs)
            if snippet:
                logfire_msg = f"User: {snippet} -> Subagent: {subagent_type}"
            else:
                logfire_msg = f"Subagent: {subagent_type}"
        else:
            logfire_msg = _describe_call(call_input_msgs, call_output_msgs)
        span_name = f"chat {call_model}"

        attrs = [
            make_attr("logfire.msg", logfire_msg),
            make_attr("logfire.span_type", "span"),
            make_attr("gen_ai.operation.name", "chat"),
            make_attr("gen_ai.system", "anthropic"),
            make_attr("gen_ai.request.model", call_model),
            make_attr("gen_ai.response.model", call_model),
            make_int_attr("gen_ai.usage.input_tokens", input_tokens),
            make_int_attr("gen_ai.usage.output_tokens", output_tokens),
            make_complex_attr("gen_ai.input.messages", call_input_msgs),
            make_complex_attr("gen_ai.output.messages", call_output_msgs),
            make_complex_attr("gen_ai.response.finish_reasons", [call_stop_reason]),
        ]

        if cost is not None:
            attrs.append(make_double_attr("operation.cost", cost))

        if out_tools:
            attrs.append(make_complex_attr("claude_code.tools_used", out_tools))
        if categories:
            attrs.append(make_complex_attr("claude_code.tool_categories", categories))
        if skill_name:
            attrs.append(make_attr("claude_code.skill", skill_name))
        if subagent_type:
            attrs.append(make_attr("claude_code.subagent_type", subagent_type))

        json_schema: dict = {
            "type": "object",
            "properties": {
                "gen_ai.input.messages": {"type": "array"},
                "gen_ai.output.messages": {"type": "array"},
            },
        }
        if out_tools:
            json_schema["properties"]["claude_code.tools_used"] = {"type": "array"}
            json_schema["properties"]["claude_code.tool_categories"] = {"type": "array"}
        attrs.append(make_complex_attr("logfire.json_schema", json_schema))

        span = build_span(trace_id, span_id, root_span_id, span_name, call_ns, call_ns, attrs)
        spans.append(span)

    return spans, new_messages, new_cost_details, usage_deltas, tools_meta


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def handle_session_start(
    inp: dict,
    state_file: str,
    lock_file: str,
    ts_nano: int,
    transcript_path: str,
    parent_span_id_from_env: str,
    trace_id: str,
    otlp_endpoint: str,
    logfire_token: str,
    session_id: str,
) -> None:
    if not acquire_lock(lock_file):
        return
    try:
        root_span_id = random_span_id()
        cwd = inp.get("cwd", "")
        model = inp.get("model", "")
        term_program = os.environ.get("TERM_PROGRAM", "")

        initial_line = 0
        if transcript_path and os.path.isfile(transcript_path):
            try:
                with open(transcript_path) as f:
                    initial_line = sum(1 for _ in f)
            except OSError:
                pass

        # Clean up stale state
        try:
            os.unlink(state_file)
        except OSError:
            pass

        state = {
            "root_span_id": root_span_id,
            "parent_span_id": parent_span_id_from_env,
            "start_time": str(ts_nano),
            "cwd": cwd,
            "model": model,
            "term_program": term_program,
            "transcript_path": transcript_path,
            "last_line": initial_line,
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "cost_details": [],
            "all_messages": [],
            "tools_meta": {"tools_used": {}, "categories": [], "skills": []},
        }
        write_state(state_file, state)

        # Emit a pending span so Logfire shows the session as in-progress
        pending_span_id = random_span_id()
        attrs = [
            make_attr("logfire.msg", "Claude Code session"),
            make_attr("logfire.span_type", "pending_span"),
            make_attr("logfire.pending_parent_id", parent_span_id_from_env or "0000000000000000"),
            make_attr("agent_name", "claude-code"),
            make_attr("gen_ai.agent.name", "claude-code"),
            make_attr("gen_ai.system", "anthropic"),
            make_attr("session.id", session_id),
        ]
        if model:
            attrs.append(make_attr("gen_ai.response.model", model))
            attrs.append(make_attr("model_name", model))
        if cwd:
            attrs.append(make_attr("session.cwd", cwd))

        span = build_span(
            trace_id,
            pending_span_id,
            root_span_id,
            "Claude Code session",
            ts_nano,
            ts_nano,
            attrs,
        )
        payload = build_otlp_envelope([span])
        send_otlp(payload, otlp_endpoint, logfire_token)
    finally:
        release_lock(lock_file)


def _merge_tools_meta(state: dict, new_meta: dict) -> None:
    """Merge new tools_meta into state's accumulated tools_meta."""
    meta = state.setdefault("tools_meta", {"tools_used": {}, "categories": [], "skills": []})
    for tool_name, count in new_meta.get("tools_used", {}).items():
        meta["tools_used"][tool_name] = meta["tools_used"].get(tool_name, 0) + count
    for cat in new_meta.get("categories", []):
        if cat not in meta["categories"]:
            meta["categories"].append(cat)
    for skill in new_meta.get("skills", []):
        if skill not in meta["skills"]:
            meta["skills"].append(skill)


def handle_stop(
    inp: dict,
    state_file: str,
    lock_file: str,
    trace_id: str,
    ts_nano: int,
    transcript_path: str,
    otlp_endpoint: str,
    logfire_token: str,
    hook_event: str = "Stop",
) -> None:
    if not acquire_lock(lock_file):
        return
    try:
        state = read_state(state_file)
        if not state:
            log_diag("warn", "Stop without state file, skipping")
            return

        root_span_id = state["root_span_id"]
        last_line = state.get("last_line", 0)
        model_default = state.get("model", "")

        subagent_type = ""
        if hook_event == "SubagentStop":
            subagent_type = inp.get("agent_type", "unknown")

        api_calls, new_total = parse_transcript_slice(transcript_path, last_line)
        if not api_calls:
            log_diag("info", "No API calls found in transcript slice")
            return

        spans, new_messages, new_cost_details, usage_deltas, tools_meta = build_child_spans_from_calls(
            api_calls, trace_id, root_span_id, model_default, ts_nano, subagent_type=subagent_type
        )

        if spans:
            payload = build_otlp_envelope(spans)
            send_otlp(payload, otlp_endpoint, logfire_token)

        # Re-read state in case of concurrent modification
        state = read_state(state_file) or state
        state["last_line"] = new_total
        state["all_messages"] = state.get("all_messages", []) + new_messages
        state["cost_details"] = state.get("cost_details", []) + new_cost_details
        for k, v in usage_deltas.items():
            state["usage"][k] = state["usage"].get(k, 0) + v
        _merge_tools_meta(state, tools_meta)
        write_state(state_file, state)
    finally:
        release_lock(lock_file)


def handle_session_end(
    inp: dict,
    state_file: str,
    lock_file: str,
    trace_id: str,
    ts_nano: int,
    transcript_path: str,
    otlp_endpoint: str,
    logfire_token: str,
    session_id: str,
) -> None:
    state = read_state(state_file)
    if not state:
        log_diag("warn", "SessionEnd without state file, skipping root span")
        return

    if not acquire_lock(lock_file):
        return
    try:
        root_span_id = state["root_span_id"]
        state_parent_span_id = state.get("parent_span_id", "")
        start_time = state.get("start_time", str(ts_nano))
        cwd = state.get("cwd", "")
        model = state.get("model", "")

        # Final transcript parse for any remaining messages
        state_tp = state.get("transcript_path", "")
        last_line = state.get("last_line", 0)
        final_tp = transcript_path or state_tp
        if final_tp and os.path.isfile(final_tp):
            remaining_calls, new_total = parse_transcript_slice(final_tp, last_line)
            if remaining_calls:
                spans, new_msgs, new_costs, usage_deltas, tools_meta = build_child_spans_from_calls(
                    remaining_calls, trace_id, root_span_id, model, ts_nano
                )
                if spans:
                    payload = build_otlp_envelope(spans)
                    send_otlp(payload, otlp_endpoint, logfire_token)

                state["last_line"] = new_total
                state["all_messages"] = state.get("all_messages", []) + new_msgs
                state["cost_details"] = state.get("cost_details", []) + new_costs
                for k, v in usage_deltas.items():
                    state["usage"][k] = state["usage"].get(k, 0) + v
                _merge_tools_meta(state, tools_meta)

        all_messages = state.get("all_messages", [])

        # Extract final_result: last text part from last assistant message
        final_result = None
        for msg in reversed(all_messages):
            if msg.get("role") == "assistant":
                for part in reversed(msg.get("parts", [])):
                    if part.get("type") == "text":
                        final_result = part.get("content")
                        break
                break

        # Build root span attributes
        attrs = [
            make_attr("logfire.msg", "Claude Code session"),
            make_attr("logfire.span_type", "span"),
            make_attr("agent_name", "claude-code"),
            make_attr("gen_ai.agent.name", "claude-code"),
            make_attr("gen_ai.system", "anthropic"),
        ]
        if model:
            attrs.append(make_attr("gen_ai.response.model", model))
            attrs.append(make_attr("model_name", model))
        attrs.append(make_attr("session.id", session_id))
        if cwd:
            attrs.append(make_attr("session.cwd", cwd))
        if final_result is not None:
            attrs.append(make_complex_attr("final_result", final_result))
        attrs.append(make_complex_attr("pydantic_ai.all_messages", all_messages))

        # Aggregated tool metadata from all Stop events
        meta = state.get("tools_meta", {})
        tools_used_counts = [
            {"tool": name, "count": count}
            for name, count in meta.get("tools_used", {}).items()
        ]
        if tools_used_counts:
            attrs.append(make_complex_attr("claude_code.tools_used", tools_used_counts))
        agg_categories = meta.get("categories", [])
        if agg_categories:
            attrs.append(make_complex_attr("claude_code.tool_categories", agg_categories))
        agg_skills = meta.get("skills", [])
        if agg_skills:
            attrs.append(make_complex_attr("claude_code.skills_used", agg_skills))

        json_schema: dict = {
            "type": "object",
            "properties": {
                "final_result": {"type": "object"},
                "pydantic_ai.all_messages": {"type": "array"},
            },
        }
        if tools_used_counts:
            json_schema["properties"]["claude_code.tools_used"] = {"type": "array"}
            json_schema["properties"]["claude_code.tool_categories"] = {"type": "array"}
        if agg_skills:
            json_schema["properties"]["claude_code.skills_used"] = {"type": "array"}
        attrs.append(make_complex_attr("logfire.json_schema", json_schema))

        span = build_span(
            trace_id,
            root_span_id,
            state_parent_span_id,
            "Claude Code session",
            int(start_time),
            ts_nano,
            attrs,
        )
        payload = build_otlp_envelope([span])
        send_otlp(payload, otlp_endpoint, logfire_token)

        try:
            os.unlink(state_file)
        except OSError:
            pass
    finally:
        release_lock(lock_file)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    global _hook_event, _session_id, _diag_log

    enable_local_log = os.environ.get("LOGFIRE_LOCAL_LOG", "false") in ("true", "1")
    enable_diagnostics = os.environ.get("LOGFIRE_DIAGNOSTICS", "false") in ("true", "1")
    log_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR", ".")) / ".claude" / "logs"

    log_file = None
    if enable_local_log:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = str(log_dir / "session-events.jsonl")

    if enable_diagnostics or enable_local_log:
        log_dir.mkdir(parents=True, exist_ok=True)
        _diag_log = str(log_dir / "diagnostics.jsonl")

    # Read stdin
    raw_input = sys.stdin.read()

    try:
        inp = json.loads(raw_input)
    except json.JSONDecodeError:
        log_diag("error", "Failed to parse hook input JSON", raw_input[:500])
        return

    _hook_event = inp.get("hook_event_name", "unknown")
    _session_id = inp.get("session_id", "unknown")

    if _hook_event in ("unknown", "parse_error") or _session_id in ("unknown", "parse_error"):
        log_diag("error", "Failed to parse hook input JSON", raw_input[:500])
        return

    # JSONL logging
    ts_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    if log_file:
        try:
            entry = {**inp, "captured_at": ts_iso}
            with open(log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except OSError:
            log_diag("error", "Failed to write JSONL log entry")

    # OTel export requires token
    logfire_token = os.environ.get("LOGFIRE_TOKEN", "")
    if not logfire_token:
        return

    base_url = os.environ.get("LOGFIRE_BASE_URL", "https://logfire-us.pydantic.dev").rstrip("/")
    otlp_endpoint = f"{base_url}/v1/traces"

    # Only process OTLP events
    if _hook_event not in OTLP_EVENTS:
        return

    session_id = inp.get("session_id", "")
    hook_event = _hook_event
    if not session_id:
        log_diag("warn", "Missing session_id, skipping OTel export")
        return

    trace_id = trace_id_from_session(session_id)

    # W3C TRACEPARENT override
    parent_span_id_from_env = ""
    traceparent = os.environ.get("TRACEPARENT", "")
    m = re.match(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$", traceparent)
    if m:
        trace_id = m.group(1)
        parent_span_id_from_env = m.group(2)
        log_diag(
            "info", "Using trace context from TRACEPARENT", f"trace_id={trace_id} parent={parent_span_id_from_env}"
        )

    ts_nano = now_nano()
    transcript_path = inp.get("transcript_path", "")

    tmpdir = os.environ.get("TMPDIR", "/tmp")
    state_file = os.path.join(tmpdir, f"claude-logfire-{session_id}.json")
    lock_file = f"{state_file}.lock"

    if hook_event == "SessionStart":
        handle_session_start(
            inp,
            state_file,
            lock_file,
            ts_nano,
            transcript_path,
            parent_span_id_from_env,
            trace_id,
            otlp_endpoint,
            logfire_token,
            session_id,
        )
    elif hook_event in ("Stop", "SubagentStop"):
        handle_stop(inp, state_file, lock_file, trace_id, ts_nano, transcript_path, otlp_endpoint, logfire_token, hook_event)
    elif hook_event == "SessionEnd":
        handle_session_end(
            inp, state_file, lock_file, trace_id, ts_nano, transcript_path, otlp_endpoint, logfire_token, session_id
        )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            log_diag("error", "Unexpected failure", str(sys.exc_info()[1]))
        except Exception:
            pass
    sys.exit(0)
