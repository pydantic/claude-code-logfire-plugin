# logfire-session-capture

Claude Code plugin that captures all session events as OpenTelemetry traces sent to [Pydantic Logfire](https://logfire.pydantic.dev). Each session becomes a trace, each hook event becomes a child span. Local JSONL logging is always active as a fallback.

## Setup

### Prerequisites

- `jq` must be installed (`brew install jq` / `apt install jq`)
- `curl` (pre-installed on macOS/Linux)

### Install the plugin

```bash
claude plugin add /path/to/logfire-session-capture
```

### Configure Logfire

Set your Logfire write token to enable trace export:

```bash
export LOGFIRE_TOKEN="your-logfire-write-token"
```

For the EU region, also set:

```bash
export LOGFIRE_BASE_URL="https://logfire-eu.pydantic.dev"
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `LOGFIRE_TOKEN` | Yes | _(none, OTel disabled)_ | Logfire write token |
| `LOGFIRE_BASE_URL` | No | `https://logfire-us.pydantic.dev` | Logfire ingest endpoint |

If `LOGFIRE_TOKEN` is not set, the plugin only writes JSONL locally with zero network overhead.

## Trace Structure

Each Claude Code session produces one trace in Logfire:

```
Trace (trace_id derived from session_id)
  └── Root span: "claude-code-session" (start=SessionStart, end=SessionEnd)
        ├── Span: "SessionStart"
        ├── Span: "UserPromptSubmit"
        ├── Span: "PreToolUse" (tool.name=Read)
        ├── Span: "PostToolUse" (tool.name=Read)
        ├── Span: "Stop"
        └── ...
```

- **trace_id** is deterministically derived from `session_id` (SHA-256), so all spans from the same session are correlated without shared state.
- **Root span** is sent at SessionEnd with its start time backdated to SessionStart. OTLP backends handle out-of-order arrival.
- **Child spans** are sent immediately as each hook fires (point-in-time, start == end).

## Events Captured

| Event | Span Attributes |
|-------|----------------|
| SessionStart | session.cwd, session.model |
| SessionEnd | session.end_reason |
| UserPromptSubmit | _(structural only)_ |
| PreToolUse | tool.name, tool.use_id |
| PostToolUse | tool.name, tool.use_id |
| Stop | _(structural only)_ |
| SubagentStop | _(structural only)_ |
| PreCompact | _(structural only)_ |
| Notification | _(structural only)_ |

All spans include `hook.event`, `session.id`, `logfire.msg`, and `logfire.span_type` attributes.

Full event payloads (tool inputs/outputs, user prompts) are written to the local JSONL log only, to avoid payload size and privacy concerns in the remote trace.

## Local Log

Events are always written as JSON Lines to `.claude/logs/session-events.jsonl` in the project directory:

```json
{"session_id":"abc123","hook_event_name":"UserPromptSubmit","user_prompt":"fix the bug","captured_at":"2026-02-12T10:30:00Z"}
```

## Error Handling

- **Logfire unreachable**: `curl` has a 5-second timeout; failures are silently ignored.
- **SessionEnd never fires**: Child spans are still linked by trace ID; the orphaned temp file is cleaned by the OS.
- **No LOGFIRE_TOKEN**: Script exits after JSONL write with zero network overhead.
- **macOS nanoseconds**: Falls back to second-precision timestamps if `date +%N` is unavailable.
