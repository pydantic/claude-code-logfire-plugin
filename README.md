# Logfire plugin for Claude Code

> [!IMPORTANT]
> New installations should use Claude Code's native OpenTelemetry integration,
> configured by `logfire-cli`, instead of this legacy hook-based session
> exporter. Native telemetry follows Claude Code's supported event schema,
> keeps project credentials out of `settings.json`, and can be removed without
> uninstalling unrelated plugins:
>
> ```bash
> uvx --refresh logfire-cli --beta agent claude configure
> uvx --refresh logfire-cli --beta agent claude status
> ```
>
> This plugin remains available for existing installations while they migrate.

A [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin that sends OpenTelemetry traces to [Pydantic Logfire](https://logfire.pydantic.dev), giving you full observability into your Claude Code sessions.

Each session becomes a trace with child spans per LLM API call, with full token usage, cost tracking, and conversation history visible in Logfire.

<!-- TODO: add Logfire screenshot here -->

## Installation

### Recommended: native Claude Code telemetry

Connect the current directory to the intended Logfire project, configure
Claude Code, then restart it:

```bash
uvx --refresh logfire-cli auth
uvx --refresh logfire-cli init use --name YOUR_PROJECT
uvx --refresh logfire-cli --beta agent claude configure
```

Use `--capture metadata-only` if prompts and tool arguments must not be sent:

```bash
uvx --refresh logfire-cli --beta agent claude configure --capture metadata-only
```

The command uses Claude Code's documented `otelHeadersHelper`; it does not put
the project token in Claude's settings. See the current
[`logfire-cli` Claude guide](https://github.com/pydantic/logfire-cli/blob/main/docs/commands/claude.md)
for status, removal, capture, and conflict behavior.

### Legacy hook plugin

### System requirements

- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed
- A [Logfire](https://logfire.pydantic.dev) project with a write token
- `python3` (3.7+) — pre-installed on macOS and most Linux distributions; uses stdlib only (no pip dependencies)

#### Install the plugin

From within Claude Code, run:

```
/plugin marketplace add pydantic/claude-code-logfire-plugin
/plugin install logfire-session-capture@pydantic-claude-code-logfire-plugin
```

#### Update the plugin

```
/plugin update logfire-session-capture@pydantic-claude-code-logfire-plugin
```

#### Set your Logfire token

```bash
export LOGFIRE_TOKEN="your-logfire-write-token"
```

Add this to your shell profile (`~/.zshrc`, `~/.bashrc`, etc.) so it persists across sessions.

For the EU region:

```bash
export LOGFIRE_BASE_URL="https://logfire-eu.pydantic.dev"
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `LOGFIRE_TOKEN` | Yes | _(none)_ | Logfire write token |
| `LOGFIRE_BASE_URL` | No | `https://logfire-us.pydantic.dev` | Logfire ingest endpoint |
| `LOGFIRE_LOCAL_LOG` | No | `false` | Set to `true` to write JSONL event logs locally |
| `LOGFIRE_DIAGNOSTICS` | No | `false` | Set to `true` to write diagnostic logs (enabled automatically when `LOGFIRE_LOCAL_LOG` is set) |
| `LOGFIRE_ENVIRONMENT` | No | _(none)_ | Sets `deployment.environment.name` on every trace (e.g. `production`, `dev`) |
| `LOGFIRE_SESSION_LABEL` | No | `Claude Code session` | Label for the root session span — useful when running multiple CC sessions in one trace |
| `OTEL_SERVICE_NAME` | No | `claude-code-plugin` | Overrides the `service.name` resource attribute |
| `OTEL_RESOURCE_ATTRIBUTES` | No | _(none)_ | Standard OTel env var for additional resource attributes, e.g. `deployment.environment.name=prod,service.instance.id=worker-1` |

Without `LOGFIRE_TOKEN`, no traces are sent. The plugin does nothing unless at least one of `LOGFIRE_TOKEN` or `LOGFIRE_LOCAL_LOG` is set.

## What you get

Every Claude Code session produces a trace in Logfire:

```
Claude Code session              <- root span (the full session)
├── chat claude-opus-4-6         <- LLM API call 1
├── chat claude-opus-4-6         <- LLM API call 2
└── chat claude-opus-4-6         <- LLM API call 3
```

Each `chat` child span includes:

- **Token usage** (`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`)
- **Cost** (`operation.cost` in USD)
- **Messages** (`gen_ai.input.messages`, `gen_ai.output.messages`)
- **Finish reason** (`gen_ai.response.finish_reasons`)

The root span carries the full conversation, so you can inspect the entire session in Logfire's trace view.

## Distributed tracing

If you call Claude Code from a Python application that already uses Logfire or OpenTelemetry, you can link the Claude Code session into your existing trace by passing a `TRACEPARENT` environment variable:

```bash
TRACEPARENT="00-<trace_id>-<parent_span_id>-01" claude --print "your prompt"
```

See [`examples/distributed-tracing.py`](examples/distributed-tracing.py) for a complete example using `logfire` and `subprocess`.

## Local JSONL log

Set `LOGFIRE_LOCAL_LOG=true` to write all hook events as JSON Lines to `.claude/logs/session-events.jsonl` in the project directory. This is off by default.

## Data collected

When `LOGFIRE_TOKEN` is set, the plugin sends the following data to Logfire as OpenTelemetry span attributes:

| Data | Span | Attribute |
|---|---|---|
| Full conversation (user prompts, assistant responses, tool calls and results) | Root span | `pydantic_ai.all_messages` |
| Per-call input/output messages | Child spans | `gen_ai.input.messages`, `gen_ai.output.messages` |
| Token counts | Child spans | `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` |
| Cost in USD | Child spans | `operation.cost` |
| Model name | Both | `gen_ai.request.model` |
| Working directory | Root span | `session.cwd` |
| Assistant thinking blocks | Child spans | Included in `gen_ai.output.messages` |

**Privacy note:** Conversation data sent to Logfire may contain sensitive information including file contents read by Claude, tool outputs, environment details, and any text in the conversation. Logfire data is stored according to [Pydantic's privacy policy](https://pydantic.dev/privacy). If this is a concern, use `LOGFIRE_LOCAL_LOG=true` without `LOGFIRE_TOKEN` to keep all data local.

## Troubleshooting

**Enable diagnostics** to see what the plugin is doing:

```bash
export LOGFIRE_DIAGNOSTICS=true
```

Diagnostic logs are written to `.claude/logs/diagnostics.jsonl` in the project directory.

**Common issues:**

- **No traces appearing in Logfire** -- Check that `LOGFIRE_TOKEN` is set and valid. Enable diagnostics to see if OTLP exports are failing.
- **Export errors (HTTP 401/403)** -- Your Logfire token may be invalid or expired. Generate a new write token in the Logfire console.
- **Export errors (HTTP 4xx/5xx)** -- Check `LOGFIRE_BASE_URL` if using a non-default region. The plugin logs HTTP status codes to stderr and diagnostics.

## Development

```bash
uv sync
uv run ruff check scripts/
uv run ruff format scripts/
```

## How it works

The plugin is a single Python script ([`scripts/log-event.py`](scripts/log-event.py), stdlib only) invoked by Claude Code hooks on every session event. On `Stop` events it parses the transcript file to extract per-API-call data (deduplicating streaming fragments) and sends OTLP/HTTP JSON to Logfire. On `SessionEnd` it sends the root span with the accumulated conversation.

State is persisted in a temp file between hook invocations. The `trace_id` is deterministically derived from `session_id` via SHA-256.

## License

MIT

## Migrating an existing installation

1. Configure and verify native telemetry with `logfire-cli`.
2. Restart Claude Code and confirm a new prompt appears in Logfire.
3. Disable or uninstall `logfire-session-capture` so the legacy hook exporter
   does not duplicate native records.

```bash
claude plugin disable logfire-session-capture@pydantic-claude-code-logfire-plugin
uvx --refresh logfire-cli --beta agent claude status
```

The old plugin's `LOGFIRE_TOKEN` is independent of the CLI-managed credential.
Remove it from your shell only after other applications no longer need it.
