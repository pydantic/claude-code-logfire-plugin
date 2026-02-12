#!/bin/bash
set -euo pipefail

# ---------------------------------------------------------------------------
# log-event.sh -- Dual-mode session event logger
#
# Mode 1 (always): Append JSONL to local log file
# Mode 2 (if LOGFIRE_TOKEN set): Send OTel spans to Logfire via OTLP/HTTP JSON
# ---------------------------------------------------------------------------

LOG_DIR="${CLAUDE_PROJECT_DIR:-.}/.claude/logs"
LOG_FILE="$LOG_DIR/session-events.jsonl"
mkdir -p "$LOG_DIR"

input=$(cat)
# macOS date doesn't support %N; detect by checking for literal 'N' in output
timestamp=$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")
if [[ "$timestamp" == *N* ]]; then
  timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
fi

# --- JSONL logging (always) ------------------------------------------------
echo "$input" | jq -c --arg ts "$timestamp" '. + {captured_at: $ts}' >> "$LOG_FILE"

# --- OTel via Logfire (if token set) ---------------------------------------
LOGFIRE_TOKEN="${LOGFIRE_TOKEN:-}"
[ -z "$LOGFIRE_TOKEN" ] && exit 0

LOGFIRE_BASE_URL="${LOGFIRE_BASE_URL:-https://logfire-us.pydantic.dev}"
LOGFIRE_BASE_URL="${LOGFIRE_BASE_URL%/}"
OTLP_ENDPOINT="${LOGFIRE_BASE_URL}/v1/traces"

# --- Helpers ---------------------------------------------------------------

now_nano() {
  # macOS date doesn't support %N; try GNU date, then python3, then fall back
  if date +%s%N 2>/dev/null | grep -qv N; then
    date +%s%N
  elif command -v python3 &>/dev/null; then
    python3 -c 'import time; print(int(time.time()*1e9))'
  else
    echo "$(date +%s)000000000"
  fi
}

random_span_id() {
  # 16 hex chars (8 bytes)
  head -c 8 /dev/urandom | xxd -p | head -c 16
}

trace_id_from_session() {
  # Deterministic 32-hex-char trace ID from session_id via SHA-256
  printf '%s' "$1" | shasum -a 256 | head -c 32
}

send_otlp() {
  local payload="$1"
  curl -s --max-time 5 \
    -X POST "$OTLP_ENDPOINT" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $LOGFIRE_TOKEN" \
    -d "$payload" >/dev/null 2>&1 || true
}

build_otlp_payload() {
  local trace_id="$1" span_id="$2" parent_span_id="$3" name="$4"
  local start_ns="$5" end_ns="$6" attrs_json="$7"

  jq -n -c \
    --arg traceId "$trace_id" \
    --arg spanId "$span_id" \
    --arg parentSpanId "$parent_span_id" \
    --arg name "$name" \
    --arg startNs "$start_ns" \
    --arg endNs "$end_ns" \
    --argjson attrs "$attrs_json" \
    '{
      resourceSpans: [{
        resource: {
          attributes: [
            {key: "service.name", value: {stringValue: "claude-code-plugin"}},
            {key: "service.version", value: {stringValue: "0.2.0"}}
          ]
        },
        scopeSpans: [{
          scope: {name: "logfire-session-capture", version: "0.2.0"},
          spans: [{
            traceId: $traceId,
            spanId: $spanId,
            parentSpanId: $parentSpanId,
            name: $name,
            kind: 1,
            startTimeUnixNano: $startNs,
            endTimeUnixNano: $endNs,
            attributes: $attrs,
            status: {code: 1}
          }]
        }]
      }]
    }'
}

make_attr() {
  local key="$1" val="$2"
  jq -n -c --arg k "$key" --arg v "$val" '{key:$k, value:{stringValue:$v}}'
}

make_int_attr() {
  local key="$1" val="$2"
  jq -n -c --arg k "$key" --arg v "$val" '{key:$k, value:{intValue:$v}}'
}

# --- Extract fields from input ---------------------------------------------

hook_event=$(echo "$input" | jq -r '.hook_event_name // empty')
session_id=$(echo "$input" | jq -r '.session_id // empty')

[ -z "$hook_event" ] || [ -z "$session_id" ] && exit 0

trace_id=$(trace_id_from_session "$session_id")
ts_nano=$(now_nano)
transcript_path=$(echo "$input" | jq -r '.transcript_path // empty')

STATE_FILE="${TMPDIR:-/tmp}/claude-logfire-${session_id}.json"

# Helper: read parent span ID from state file
read_root_span_id() {
  if [ -f "$STATE_FILE" ]; then
    jq -r '.root_span_id' "$STATE_FILE"
  else
    echo ""
  fi
}

# Helper: extract last assistant response from new transcript lines
# Uses last_line tracking (a la LangChain) to read only new lines since last invocation
extract_assistant_response() {
  local tp="$1"
  [ -z "$tp" ] || [ ! -f "$tp" ] && return

  local last_line=0
  if [ -f "$STATE_FILE" ]; then
    last_line=$(jq -r '.last_line // 0' "$STATE_FILE")
  fi

  local new_lines
  new_lines=$(awk -v start="$last_line" 'NR > start && NF' "$tp" 2>/dev/null || true)

  # Update last_line in state file
  local total_lines
  total_lines=$(wc -l < "$tp" 2>/dev/null | tr -d ' ')
  if [ -f "$STATE_FILE" ]; then
    local updated_state
    updated_state=$(jq -c --arg ll "$total_lines" '.last_line = ($ll | tonumber)' "$STATE_FILE")
    echo "$updated_state" > "$STATE_FILE"
  fi

  [ -z "$new_lines" ] && return

  # Find last assistant text response from the new lines
  local last_assistant_line
  last_assistant_line=$(echo "$new_lines" | grep '"type": *"assistant"' 2>/dev/null | tail -1 || true)
  [ -z "$last_assistant_line" ] && return

  echo "$last_assistant_line" \
    | jq -r '[.message.content[]? | select(.type=="text") | .text] | join("\n")' 2>/dev/null \
    | head -c 10000 || true
}

# --- Build span attributes per event type ----------------------------------

case "$hook_event" in
  SessionStart)
    root_span_id=$(random_span_id)
    cwd=$(echo "$input" | jq -r '.cwd // empty')
    model=$(echo "$input" | jq -r '.model // empty')
    source=$(echo "$input" | jq -r '.source // empty')

    # Snapshot current transcript length so we only read new lines on subsequent hooks
    initial_line=0
    if [ -n "$transcript_path" ] && [ -f "$transcript_path" ]; then
      initial_line=$(wc -l < "$transcript_path" | tr -d ' ')
    fi

    # Persist state for subsequent hooks
    jq -n -c \
      --arg root_span_id "$root_span_id" \
      --arg start_time "$ts_nano" \
      --arg cwd "$cwd" \
      --arg model "$model" \
      --argjson last_line "$initial_line" \
      '{root_span_id:$root_span_id, start_time:$start_time, cwd:$cwd, model:$model, last_line:$last_line}' \
      > "$STATE_FILE"

    attrs="[$(make_attr "hook.event" "$hook_event")"
    attrs="$attrs,$(make_attr "session.id" "$session_id")"
    attrs="$attrs,$(make_attr "logfire.msg" "SessionStart")"
    attrs="$attrs,$(make_attr "logfire.span_type" "span")"
    [ -n "$cwd" ] && attrs="$attrs,$(make_attr "session.cwd" "$cwd")"
    [ -n "$model" ] && attrs="$attrs,$(make_attr "session.model" "$model")"
    [ -n "$source" ] && attrs="$attrs,$(make_attr "session.source" "$source")"
    attrs="$attrs]"

    span_id=$(random_span_id)
    payload=$(build_otlp_payload "$trace_id" "$span_id" "$root_span_id" "SessionStart" "$ts_nano" "$ts_nano" "$attrs")
    send_otlp "$payload"
    ;;

  SessionEnd)
    end_reason=$(echo "$input" | jq -r '.reason // empty')

    if [ -f "$STATE_FILE" ]; then
      state=$(cat "$STATE_FILE")
      root_span_id=$(echo "$state" | jq -r '.root_span_id')
      start_time=$(echo "$state" | jq -r '.start_time')
      cwd=$(echo "$state" | jq -r '.cwd // empty')
      model=$(echo "$state" | jq -r '.model // empty')
    else
      root_span_id=$(random_span_id)
      start_time="$ts_nano"
      cwd=""
      model=""
    fi

    child_attrs="[$(make_attr "hook.event" "$hook_event")"
    child_attrs="$child_attrs,$(make_attr "session.id" "$session_id")"
    child_attrs="$child_attrs,$(make_attr "logfire.msg" "SessionEnd")"
    child_attrs="$child_attrs,$(make_attr "logfire.span_type" "span")"
    [ -n "$end_reason" ] && child_attrs="$child_attrs,$(make_attr "session.end_reason" "$end_reason")"
    child_attrs="$child_attrs]"

    child_span_id=$(random_span_id)
    child_payload=$(build_otlp_payload "$trace_id" "$child_span_id" "$root_span_id" "SessionEnd" "$ts_nano" "$ts_nano" "$child_attrs")
    send_otlp "$child_payload"

    # Root span covering entire session
    root_attrs="[$(make_attr "hook.event" "session")"
    root_attrs="$root_attrs,$(make_attr "session.id" "$session_id")"
    root_attrs="$root_attrs,$(make_attr "logfire.msg" "claude-code-session")"
    root_attrs="$root_attrs,$(make_attr "logfire.span_type" "span")"
    [ -n "$cwd" ] && root_attrs="$root_attrs,$(make_attr "session.cwd" "$cwd")"
    [ -n "$model" ] && root_attrs="$root_attrs,$(make_attr "session.model" "$model")"
    [ -n "$end_reason" ] && root_attrs="$root_attrs,$(make_attr "session.end_reason" "$end_reason")"
    root_attrs="$root_attrs]"

    root_payload=$(build_otlp_payload "$trace_id" "$root_span_id" "" "claude-code-session" "$start_time" "$ts_nano" "$root_attrs")
    send_otlp "$root_payload"

    rm -f "$STATE_FILE"
    ;;

  UserPromptSubmit)
    root_span_id=$(read_root_span_id)
    prompt=$(echo "$input" | jq -r '.prompt // empty')

    logfire_msg="UserPromptSubmit"
    [ -n "$prompt" ] && logfire_msg="UserPromptSubmit: ${prompt:0:200}"

    attrs="[$(make_attr "hook.event" "$hook_event")"
    attrs="$attrs,$(make_attr "session.id" "$session_id")"
    attrs="$attrs,$(make_attr "logfire.msg" "$logfire_msg")"
    attrs="$attrs,$(make_attr "logfire.span_type" "span")"
    [ -n "$prompt" ] && attrs="$attrs,$(make_attr "user.prompt" "$prompt")"
    attrs="$attrs]"

    span_id=$(random_span_id)
    payload=$(build_otlp_payload "$trace_id" "$span_id" "$root_span_id" "UserPromptSubmit" "$ts_nano" "$ts_nano" "$attrs")
    send_otlp "$payload"
    ;;

  Stop|SubagentStop)
    root_span_id=$(read_root_span_id)

    # Extract assistant response from transcript
    response=$(extract_assistant_response "$transcript_path")

    logfire_msg="$hook_event"
    [ -n "$response" ] && logfire_msg="${hook_event}: ${response:0:200}"

    attrs="[$(make_attr "hook.event" "$hook_event")"
    attrs="$attrs,$(make_attr "session.id" "$session_id")"
    attrs="$attrs,$(make_attr "logfire.msg" "$logfire_msg")"
    attrs="$attrs,$(make_attr "logfire.span_type" "span")"
    [ -n "$response" ] && attrs="$attrs,$(make_attr "assistant.response" "$response")"

    if [ "$hook_event" = "SubagentStop" ]; then
      agent_type=$(echo "$input" | jq -r '.agent_type // empty')
      [ -n "$agent_type" ] && attrs="$attrs,$(make_attr "agent.type" "$agent_type")"
    fi

    attrs="$attrs]"

    span_id=$(random_span_id)
    payload=$(build_otlp_payload "$trace_id" "$span_id" "$root_span_id" "$hook_event" "$ts_nano" "$ts_nano" "$attrs")
    send_otlp "$payload"
    ;;

  PreToolUse)
    root_span_id=$(read_root_span_id)
    tool_name=$(echo "$input" | jq -r '.tool_name // empty')
    tool_use_id=$(echo "$input" | jq -r '.tool_use_id // empty')
    tool_input=$(echo "$input" | jq -c '.tool_input // empty')

    logfire_msg="PreToolUse"
    [ -n "$tool_name" ] && logfire_msg="PreToolUse: ${tool_name}"

    attrs="[$(make_attr "hook.event" "$hook_event")"
    attrs="$attrs,$(make_attr "session.id" "$session_id")"
    attrs="$attrs,$(make_attr "logfire.msg" "$logfire_msg")"
    attrs="$attrs,$(make_attr "logfire.span_type" "span")"
    [ -n "$tool_name" ] && attrs="$attrs,$(make_attr "tool.name" "$tool_name")"
    [ -n "$tool_use_id" ] && attrs="$attrs,$(make_attr "tool.use_id" "$tool_use_id")"
    [ -n "$tool_input" ] && [ "$tool_input" != '""' ] && attrs="$attrs,$(make_attr "tool.input" "$tool_input")"
    attrs="$attrs]"

    span_id=$(random_span_id)
    payload=$(build_otlp_payload "$trace_id" "$span_id" "$root_span_id" "PreToolUse" "$ts_nano" "$ts_nano" "$attrs")
    send_otlp "$payload"
    ;;

  PostToolUse)
    root_span_id=$(read_root_span_id)
    tool_name=$(echo "$input" | jq -r '.tool_name // empty')
    tool_use_id=$(echo "$input" | jq -r '.tool_use_id // empty')
    tool_input=$(echo "$input" | jq -c '.tool_input // empty')
    # Truncate tool_response to 10k chars to avoid huge payloads
    tool_response=$(echo "$input" | jq -c '.tool_response // empty' | head -c 10000)

    logfire_msg="PostToolUse"
    [ -n "$tool_name" ] && logfire_msg="PostToolUse: ${tool_name}"

    attrs="[$(make_attr "hook.event" "$hook_event")"
    attrs="$attrs,$(make_attr "session.id" "$session_id")"
    attrs="$attrs,$(make_attr "logfire.msg" "$logfire_msg")"
    attrs="$attrs,$(make_attr "logfire.span_type" "span")"
    [ -n "$tool_name" ] && attrs="$attrs,$(make_attr "tool.name" "$tool_name")"
    [ -n "$tool_use_id" ] && attrs="$attrs,$(make_attr "tool.use_id" "$tool_use_id")"
    [ -n "$tool_input" ] && [ "$tool_input" != '""' ] && attrs="$attrs,$(make_attr "tool.input" "$tool_input")"
    [ -n "$tool_response" ] && [ "$tool_response" != '""' ] && attrs="$attrs,$(make_attr "tool.response" "$tool_response")"
    attrs="$attrs]"

    span_id=$(random_span_id)
    payload=$(build_otlp_payload "$trace_id" "$span_id" "$root_span_id" "PostToolUse" "$ts_nano" "$ts_nano" "$attrs")
    send_otlp "$payload"
    ;;

  Notification)
    root_span_id=$(read_root_span_id)
    message=$(echo "$input" | jq -r '.message // empty')
    notification_type=$(echo "$input" | jq -r '.notification_type // empty')

    logfire_msg="Notification"
    [ -n "$message" ] && logfire_msg="Notification: ${message:0:200}"

    attrs="[$(make_attr "hook.event" "$hook_event")"
    attrs="$attrs,$(make_attr "session.id" "$session_id")"
    attrs="$attrs,$(make_attr "logfire.msg" "$logfire_msg")"
    attrs="$attrs,$(make_attr "logfire.span_type" "span")"
    [ -n "$message" ] && attrs="$attrs,$(make_attr "notification.message" "$message")"
    [ -n "$notification_type" ] && attrs="$attrs,$(make_attr "notification.type" "$notification_type")"
    attrs="$attrs]"

    span_id=$(random_span_id)
    payload=$(build_otlp_payload "$trace_id" "$span_id" "$root_span_id" "Notification" "$ts_nano" "$ts_nano" "$attrs")
    send_otlp "$payload"
    ;;

  *)
    root_span_id=$(read_root_span_id)

    attrs="[$(make_attr "hook.event" "$hook_event")"
    attrs="$attrs,$(make_attr "session.id" "$session_id")"
    attrs="$attrs,$(make_attr "logfire.msg" "$hook_event")"
    attrs="$attrs,$(make_attr "logfire.span_type" "span")"
    attrs="$attrs]"

    span_id=$(random_span_id)
    payload=$(build_otlp_payload "$trace_id" "$span_id" "$root_span_id" "$hook_event" "$ts_nano" "$ts_nano" "$attrs")
    send_otlp "$payload"
    ;;
esac
