# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Claude Code plugin that captures sessions and exports pydantic-ai compatible OTel traces to Pydantic Logfire, with local JSONL fallback. The plugin is a single Python 3 script (stdlib only, no external dependencies).

## Architecture

The entire plugin is a single Python script (`scripts/log-event.py`) invoked by every hook event defined in `hooks/hooks.json`. The plugin manifest lives at `.claude-plugin/plugin.json`.

**Data flow:** Claude Code hook fires -> stdin JSON piped to `log-event.py` -> appends JSONL locally -> if `LOGFIRE_TOKEN` set and event is SessionStart/Stop/SubagentStop/SessionEnd, builds OTLP/HTTP JSON payload and sends via `urllib`.

**Session state:** A temp file (`$TMPDIR/claude-logfire-{session_id}.json`) persists the root span ID, start time, transcript line offset, accumulated messages, usage totals, and cost details between hook invocations. Created on `SessionStart`, deleted on `SessionEnd`.

**Trace correlation:** `trace_id` is deterministically derived from `session_id` via SHA-256.

**Span hierarchy (pydantic-ai style):**
```
agent run (root span)              <- the session (emitted on SessionEnd)
├── chat claude-opus-4-6           <- LLM API call 1 (emitted on Stop)
├── chat claude-opus-4-6           <- LLM API call 2 (emitted on Stop)
└── chat claude-opus-4-6           <- LLM API call 3 (emitted on Stop)
```

**OTLP processing model:**

| Hook Event | JSONL | OTLP |
|---|---|---|
| SessionStart | yes | pending root span (`logfire.span_type=pending_span`) |
| Stop / SubagentStop | yes | "chat {model}" child spans per LLM API call |
| SessionEnd | yes | finalized root "agent run" span with all_messages, usage, cost |
| All other events | yes | none (early exit) |

**Transcript parsing:** On Stop events, the script reads new transcript lines (since last offset), deduplicates streaming fragments by `message.id`, identifies API call boundaries, and converts messages to pydantic-ai format (tool_use -> tool_call, tool_result -> tool_call_response, end_turn -> stop).

## Dependencies

- `python3` (3.7+) — the only external dependency; uses stdlib only (`json`, `urllib.request`, `hashlib`, `os`, `time`, `tempfile`)

## Testing

No automated tests. To test manually:

```bash
# Full lifecycle test
> /tmp/test-transcript.jsonl
echo '{"hook_event_name":"SessionStart","session_id":"test123","cwd":"/tmp","model":"claude-opus-4-6","transcript_path":"/tmp/test-transcript.jsonl"}' | LOGFIRE_TOKEN=your-token python3 scripts/log-event.py

# Write mock transcript lines, then:
echo '{"hook_event_name":"Stop","session_id":"test123","transcript_path":"/tmp/test-transcript.jsonl"}' | LOGFIRE_TOKEN=your-token python3 scripts/log-event.py

echo '{"hook_event_name":"SessionEnd","session_id":"test123"}' | LOGFIRE_TOKEN=your-token python3 scripts/log-event.py
```

## Key Design Decisions

- OTLP spans use pydantic-ai attribute conventions (`gen_ai.input.messages`, `gen_ai.output.messages`, `pydantic_ai.all_messages`, `logfire.metrics`) so Logfire renders them identically to pydantic-ai agent traces.
- Complex attributes use `to_otlp_anyvalue` which recursively converts Python values to OTLP AnyValue format (kvlistValue for dicts, arrayValue for lists). Checks `bool` before `int` since `bool` is a subclass of `int` in Python.
- `logfire.json_schema` attribute tells Logfire which attributes contain structured data vs plain strings.
- Stop events parse the transcript to extract per-API-call data; all state accumulates in the temp file across multiple Stop events in a session.
- `build_child_spans_from_calls` is a shared function used by both Stop and SessionEnd handlers, eliminating code duplication.
- Atomic state writes use `tempfile.mkstemp()` + `os.replace()` for guaranteed atomic overwrite on all platforms.
- Cross-platform locking uses `os.mkdir()` (atomic on all POSIX systems).
- All HTTP calls have a 5-second timeout and failures are silently ignored.
