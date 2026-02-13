#!/bin/bash
set -euo pipefail

# ---------------------------------------------------------------------------
# log-event.sh -- Pydantic-AI compatible OTel trace exporter for Claude Code
#
# Mode 1 (if LOGFIRE_LOCAL_LOG set): Append JSONL to local log file
# Mode 2 (if LOGFIRE_TOKEN set): Send OTel spans to Logfire via OTLP/HTTP JSON
#
# Trace hierarchy (pydantic-ai style):
#   Claude Code session (root span)              <- the session
#   +-- chat claude-opus-4-6           <- LLM API call 1
#   +-- chat claude-opus-4-6           <- LLM API call 2
#   ...
# ---------------------------------------------------------------------------

ENABLE_LOCAL_LOG="${LOGFIRE_LOCAL_LOG:-false}"
ENABLE_DIAGNOSTICS="${LOGFIRE_DIAGNOSTICS:-false}"
LOG_DIR="${CLAUDE_PROJECT_DIR:-.}/.claude/logs"

if [ "$ENABLE_LOCAL_LOG" = "true" ] || [ "$ENABLE_LOCAL_LOG" = "1" ]; then
  LOG_FILE="$LOG_DIR/session-events.jsonl"
  mkdir -p "$LOG_DIR"
else
  LOG_FILE=""
fi

if [ "$ENABLE_DIAGNOSTICS" = "true" ] || [ "$ENABLE_DIAGNOSTICS" = "1" ] \
   || [ "$ENABLE_LOCAL_LOG" = "true" ] || [ "$ENABLE_LOCAL_LOG" = "1" ]; then
  DIAG_LOG="$LOG_DIR/diagnostics.jsonl"
  mkdir -p "$LOG_DIR"
else
  DIAG_LOG=""
fi

# --- Diagnostics -----------------------------------------------------------

log_diag() {
  [ -z "$DIAG_LOG" ] && return 0
  local level="$1" msg="$2"
  shift 2
  local extra="${1:-}"
  local ts
  ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  local event="${_HOOK_EVENT:-unknown}"
  local sid="${_SESSION_ID:-unknown}"
  if [ -n "$extra" ]; then
    jq -n -c \
      --arg ts "$ts" --arg level "$level" --arg msg "$msg" \
      --arg event "$event" --arg sid "$sid" --arg extra "$extra" \
      '{timestamp:$ts, level:$level, hook_event:$event, session_id:$sid, message:$msg, detail:$extra}' \
      >> "$DIAG_LOG" 2>/dev/null || true
  else
    jq -n -c \
      --arg ts "$ts" --arg level "$level" --arg msg "$msg" \
      --arg event "$event" --arg sid "$sid" \
      '{timestamp:$ts, level:$level, hook_event:$event, session_id:$sid, message:$msg}' \
      >> "$DIAG_LOG" 2>/dev/null || true
  fi
}

trap 'log_diag "error" "Unexpected failure at line $LINENO" "${BASH_COMMAND:-unknown}"' ERR

input=$(cat)

_HOOK_EVENT=$(echo "$input" | jq -r '.hook_event_name // "unknown"' 2>/dev/null || echo "parse_error")
_SESSION_ID=$(echo "$input" | jq -r '.session_id // "unknown"' 2>/dev/null || echo "parse_error")

if [ "$_HOOK_EVENT" = "parse_error" ] || [ "$_SESSION_ID" = "parse_error" ]; then
  log_diag "error" "Failed to parse hook input JSON" "${input:0:500}"
  exit 0
fi

timestamp=$(date -u +"%Y-%m-%dT%H:%M:%S.%3NZ")
if [[ "$timestamp" == *N* ]]; then
  timestamp=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
fi

# --- JSONL logging (opt-in via LOGFIRE_LOCAL_LOG) --------------------------
if [ -n "$LOG_FILE" ]; then
  if ! echo "$input" | jq -c --arg ts "$timestamp" '. + {captured_at: $ts}' >> "$LOG_FILE" 2>/dev/null; then
    log_diag "error" "Failed to write JSONL log entry"
  fi
fi

# --- OTel via Logfire (if token set) ---------------------------------------
LOGFIRE_TOKEN="${LOGFIRE_TOKEN:-}"
[ -z "$LOGFIRE_TOKEN" ] && exit 0

LOGFIRE_BASE_URL="${LOGFIRE_BASE_URL:-https://logfire-us.pydantic.dev}"
LOGFIRE_BASE_URL="${LOGFIRE_BASE_URL%/}"
OTLP_ENDPOINT="${LOGFIRE_BASE_URL}/v1/traces"

# --- Non-OTLP events: exit early ------------------------------------------
case "$_HOOK_EVENT" in
  SessionStart|Stop|SubagentStop|SessionEnd) ;;
  *)
    exit 0
    ;;
esac

# --- Helpers ---------------------------------------------------------------

now_nano() {
  if date +%s%N 2>/dev/null | grep -qv N; then
    date +%s%N
  elif command -v python3 &>/dev/null; then
    python3 -c 'import time; print(int(time.time()*1e9))'
  else
    echo "$(date +%s)000000000"
  fi
}

random_span_id() {
  head -c 8 /dev/urandom | xxd -p | head -c 16
}

trace_id_from_session() {
  printf '%s' "$1" | shasum -a 256 | head -c 32
}

send_otlp() {
  local payload="$1"
  local http_code
  http_code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    -X POST "$OTLP_ENDPOINT" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $LOGFIRE_TOKEN" \
    -d "$payload" 2>/dev/null) || {
    log_diag "warn" "curl failed (network/timeout)"
    echo "[logfire-plugin] OTLP export failed (network/timeout)" >&2
    return 0
  }
  if [ "$http_code" -ge 400 ] 2>/dev/null; then
    log_diag "warn" "OTLP export failed" "http_status=$http_code"
    echo "[logfire-plugin] OTLP export failed (HTTP $http_code)" >&2
  fi
}

# Build a complete OTLP payload for one or more spans.
# $1 = JSON array of span objects
build_otlp_envelope() {
  local spans_json="$1"
  jq -n -c \
    --argjson spans "$spans_json" \
    '{
      resourceSpans: [{
        resource: {
          attributes: [
            {key: "service.name", value: {stringValue: "claude-code-plugin"}},
            {key: "service.version", value: {stringValue: "0.3.0"}}
          ]
        },
        scopeSpans: [{
          scope: {name: "claude-code-logfire", version: "0.3.0"},
          spans: $spans
        }]
      }]
    }'
}

# Build a single span object (no envelope).
build_span() {
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
      traceId: $traceId,
      spanId: $spanId,
      parentSpanId: $parentSpanId,
      name: $name,
      kind: 1,
      startTimeUnixNano: $startNs,
      endTimeUnixNano: $endNs,
      attributes: $attrs,
      status: {code: 1}
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

make_double_attr() {
  local key="$1" val="$2"
  jq -n -c --arg k "$key" --argjson v "$val" '{key:$k, value:{doubleValue:$v}}'
}

# Convert arbitrary JSON to OTLP AnyValue and wrap as a named attribute
make_complex_attr() {
  local key="$1" json_val="$2"
  jq -n -c --arg k "$key" --argjson v "$json_val" '
    def to_otlp:
      if type == "string" then {stringValue: .}
      elif type == "number" then
        if . == floor and . < 9007199254740992 and . > -9007199254740992 then {intValue: tostring} else {doubleValue: .} end
      elif type == "boolean" then {boolValue: .}
      elif type == "null" then {stringValue: ""}
      elif type == "array" then {arrayValue: {values: [.[] | to_otlp]}}
      elif type == "object" then {kvlistValue: {values: [to_entries[] | {key: .key, value: (.value | to_otlp)}]}}
      else {stringValue: tostring}
      end;
    {key: $k, value: ($v | to_otlp)}
  '
}

# Calculate cost for a single LLM call. Outputs a JSON number.
calculate_cost() {
  local model="$1" input_tokens="$2" output_tokens="$3"
  local cache_creation="${4:-0}" cache_read="${5:-0}"

  local input_price=0 output_price=0
  case "${model:-}" in
    *opus*)   input_price="0.000015";  output_price="0.000075" ;;
    *sonnet*) input_price="0.000003";  output_price="0.000015" ;;
    *haiku*)  input_price="0.0000008"; output_price="0.000004" ;;
  esac

  if [ "$input_price" = "0" ]; then
    echo "null"
    return 0
  fi

  jq -n \
    --argjson input "${input_tokens:-0}" \
    --argjson output "${output_tokens:-0}" \
    --argjson cache_create "${cache_creation:-0}" \
    --argjson cache_read "${cache_read:-0}" \
    --argjson ip "$input_price" \
    --argjson op "$output_price" \
    '($input * $ip) + ($cache_create * $ip * 1.25) + ($cache_read * $ip * 0.1) + ($output * $op)' 2>/dev/null || echo "null"
}

# --- Extract fields from input ---------------------------------------------

hook_event=$(echo "$input" | jq -r '.hook_event_name // empty')
session_id=$(echo "$input" | jq -r '.session_id // empty')

if [ -z "$hook_event" ] || [ -z "$session_id" ]; then
  log_diag "warn" "Missing hook_event or session_id, skipping OTel export"
  exit 0
fi

trace_id=$(trace_id_from_session "$session_id")
parent_span_id_from_env=""

# W3C TRACEPARENT: allows external callers to propagate their trace context
# Format: 00-{trace_id:32hex}-{parent_span_id:16hex}-{flags:2hex}
if [[ "${TRACEPARENT:-}" =~ ^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$ ]]; then
  trace_id="${BASH_REMATCH[1]}"
  parent_span_id_from_env="${BASH_REMATCH[2]}"
  log_diag "info" "Using trace context from TRACEPARENT" "trace_id=$trace_id parent=$parent_span_id_from_env"
fi

ts_nano=$(now_nano)
transcript_path=$(echo "$input" | jq -r '.transcript_path // empty')

STATE_FILE="${TMPDIR:-/tmp}/claude-logfire-${session_id}.json"
LOCK_FILE="${STATE_FILE}.lock"

# Atomic state file update: write to a unique temp file, then mv (POSIX atomic rename).
update_state() {
  local tmpfile
  tmpfile=$(mktemp "${STATE_FILE}.XXXXXX") || return 1
  if "$@" > "$tmpfile" 2>/dev/null; then
    mv "$tmpfile" "$STATE_FILE"
  else
    rm -f "$tmpfile"
    return 1
  fi
}

# Cross-platform session lock using mkdir (atomic on all POSIX systems).
# flock is Linux-only; mkdir works on macOS too.
acquire_lock() {
  local attempts=0
  while ! mkdir "$LOCK_FILE" 2>/dev/null; do
    # Stale lock cleanup: if lock dir is older than 30s, previous holder likely crashed
    if [ -d "$LOCK_FILE" ]; then
      local lock_age
      lock_age=$(( $(date +%s) - $(stat -f %m "$LOCK_FILE" 2>/dev/null || stat -c %Y "$LOCK_FILE" 2>/dev/null || echo 0) ))
      if [ "$lock_age" -gt 30 ]; then
        rmdir "$LOCK_FILE" 2>/dev/null || true
        continue
      fi
    fi
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 50 ]; then
      log_diag "warn" "Could not acquire session lock after 5s"
      return 1
    fi
    sleep 0.1
  done
}

release_lock() {
  rmdir "$LOCK_FILE" 2>/dev/null || true
}

# --- Parse transcript slice ------------------------------------------------
# Reads new transcript lines since last_line, deduplicates streaming fragments,
# and returns one JSON object per LLM API call with pydantic-ai formatted messages.

parse_transcript_slice() {
  local tp="$1" last_line="$2"

  if [ -z "$tp" ] || [ ! -f "$tp" ]; then
    echo '[]'
    return 0
  fi

  local max_attempts="${TRANSCRIPT_READ_RETRIES:-20}"
  local retry_delay_s="${TRANSCRIPT_READ_DELAY_SECONDS:-0.1}"
  local attempt=0

  while [ "$attempt" -le "$max_attempts" ]; do
    local total_lines
    total_lines=$(wc -l < "$tp" 2>/dev/null | tr -d ' ')

    if [ "$total_lines" -le "$last_line" ]; then
      if [ "$attempt" -lt "$max_attempts" ]; then
        sleep "$retry_delay_s"
        attempt=$((attempt + 1))
        continue
      fi
      echo '[]'
      return 0
    fi

    local start_line=$((last_line + 1))
    local result
    result=$(sed -n "${start_line},${total_lines}p" "$tp" 2>/dev/null \
      | jq -s -c '

        # Keep only user and assistant lines
        [.[] | select(.type == "user" or .type == "assistant")] |

        # Deduplicate assistant streaming fragments: keep last per message.id
        # User messages have no message.id so they get unique keys
        group_by(
          if .type == "assistant" then (.message.id // .uuid)
          else .uuid
          end
        ) | [.[] | last] |

        # Sort by timestamp
        sort_by(.timestamp) |

        # Identify LLM API call boundaries.
        # After dedup, each unique assistant message.id = one completed API call.
        # stop_reason may be null in transcript (streaming); infer from content types.

        # Helper: determine finish reason from assistant message
        def infer_finish_reason:
          if .message.stop_reason == "end_turn" then "stop"
          elif .message.stop_reason == "tool_use" then "tool_call"
          elif .message.stop_reason != null then .message.stop_reason
          # Infer from content types when stop_reason is null
          elif ([.message.content[]? | .type] | any(. == "tool_use")) then "tool_call"
          else "stop"
          end;

        . as $all |
        [$all[] | select(.type == "assistant")] as $assistants |

        if ($assistants | length) == 0 then []
        else
          # Group user messages with following assistant message
          [foreach $all[] as $line (
            {calls: [], current_users: []};

            if $line.type == "user" then
              .current_users += [$line]
            elif $line.type == "assistant" then
              .calls += [{
                users: .current_users,
                assistant: $line
              }] |
              .current_users = []
            else .
            end;

            .
          )] | last | .calls |

          # Convert each call to pydantic-ai format
          [.[] | .assistant as $asst | {
            model: $asst.message.model,
            timestamp: $asst.timestamp,
            stop_reason: ($asst | infer_finish_reason),
            usage: $asst.message.usage,

            input_messages: [.users[] | {
              role: "user",
              parts: [
                if (.message.content | type) == "string" then
                  {type: "text", content: .message.content}
                elif (.message.content | type) == "array" then
                  .message.content[] |
                  if .type == "tool_result" then
                    {
                      type: "tool_call_response",
                      id: .tool_use_id,
                      name: (.name // null),
                      result: (if (.content | type) == "string" then
                                ((.content | fromjson?) // .content)
                               else .content end)
                    }
                  elif .type == "text" then
                    {type: "text", content: (.text // .content // "")}
                  else
                    {type: "text", content: (. | tostring)}
                  end
                else
                  {type: "text", content: (.message.content | tostring)}
                end
              ]
            }],

            output_messages: [{
              role: "assistant",
              parts: [$asst.message.content[] |
                if .type == "text" then
                  {type: "text", content: .text}
                elif .type == "thinking" then
                  {type: "thinking", thinking: .thinking}
                elif .type == "tool_use" then
                  {type: "tool_call", id: .id, name: .name, arguments: (.input | tojson)}
                else empty
                end
              ],
              finish_reason: ($asst | infer_finish_reason)
            }]
          }]
        end
      ' 2>/dev/null) || {
      if [ "$attempt" -lt "$max_attempts" ]; then
        sleep "$retry_delay_s"
        attempt=$((attempt + 1))
        continue
      fi
      log_diag "error" "jq pipeline failed during transcript parsing"
      echo '[]'
      return 0
    }

    # Update state with new line count
    if [ -f "$STATE_FILE" ]; then
      update_state jq -c --arg ll "$total_lines" '.last_line = ($ll | tonumber)' "$STATE_FILE"
    fi

    if [ "$result" = "[]" ] && [ "$attempt" -lt "$max_attempts" ]; then
      sleep "$retry_delay_s"
      attempt=$((attempt + 1))
      continue
    fi

    echo "$result"
    return 0
  done

  echo '[]'
}


# --- Build span attributes per event type ----------------------------------

case "$hook_event" in
  SessionStart)
    root_span_id=$(random_span_id)
    cwd=$(echo "$input" | jq -r '.cwd // empty')
    model=$(echo "$input" | jq -r '.model // empty')
    term_program="${TERM_PROGRAM:-}"

    initial_line=0
    if [ -n "$transcript_path" ] && [ -f "$transcript_path" ]; then
      initial_line=$(wc -l < "$transcript_path" | tr -d ' ')
    fi

    # Clean up stale state from crashed sessions
    rm -f "$STATE_FILE"

    jq -n -c \
      --arg root_span_id "$root_span_id" \
      --arg parent_span_id "$parent_span_id_from_env" \
      --arg start_time "$ts_nano" \
      --arg cwd "$cwd" \
      --arg model "$model" \
      --arg term_program "$term_program" \
      --arg transcript_path "$transcript_path" \
      --argjson last_line "$initial_line" \
      '{
        root_span_id: $root_span_id,
        parent_span_id: $parent_span_id,
        start_time: $start_time,
        cwd: $cwd,
        model: $model,
        term_program: $term_program,
        transcript_path: $transcript_path,
        last_line: $last_line,
        usage: {input_tokens:0, output_tokens:0, cache_creation_input_tokens:0, cache_read_input_tokens:0},
        cost_details: [],
        all_messages: []
      }' > "$STATE_FILE"

    ;;

  Stop|SubagentStop)
    if [ ! -f "$STATE_FILE" ]; then
      log_diag "warn" "Stop without state file, skipping"
      exit 0
    fi

    acquire_lock || exit 0
    trap 'release_lock' EXIT

    state=$(cat "$STATE_FILE")
    root_span_id=$(echo "$state" | jq -r '.root_span_id')
    last_line=$(echo "$state" | jq -r '.last_line // 0')
    model_default=$(echo "$state" | jq -r '.model // empty')

    # Parse transcript for LLM API calls since last read
    api_calls=$(parse_transcript_slice "$transcript_path" "$last_line")
    num_calls=$(echo "$api_calls" | jq 'length')

    if [ "$num_calls" -eq 0 ]; then
      log_diag "info" "No API calls found in transcript slice"
      exit 0
    fi

    # Process each API call: build child "chat" spans and accumulate state
    spans_json="[]"
    accumulated_messages="[]"
    accumulated_cost_details="[]"
    total_input=0
    total_output=0
    total_cache_creation=0
    total_cache_read=0

    for i in $(seq 0 $((num_calls - 1))); do
      call=$(echo "$api_calls" | jq -c ".[$i]")
      call_model=$(echo "$call" | jq -r '.model // empty')
      [ -z "$call_model" ] && call_model="$model_default"
      call_stop_reason=$(echo "$call" | jq -r '.stop_reason // "stop"')
      call_timestamp=$(echo "$call" | jq -r '.timestamp // empty')

      # Usage — input_tokens from the API excludes cached tokens, so sum all three
      raw_input=$(echo "$call" | jq -r '.usage.input_tokens // 0')
      output_tokens=$(echo "$call" | jq -r '.usage.output_tokens // 0')
      cache_creation=$(echo "$call" | jq -r '.usage.cache_creation_input_tokens // 0')
      cache_read=$(echo "$call" | jq -r '.usage.cache_read_input_tokens // 0')
      input_tokens=$((raw_input + cache_creation + cache_read))

      total_input=$((total_input + input_tokens))
      total_output=$((total_output + output_tokens))
      total_cache_creation=$((total_cache_creation + cache_creation))
      total_cache_read=$((total_cache_read + cache_read))

      # Cost
      cost=$(calculate_cost "$call_model" "$raw_input" "$output_tokens" "$cache_creation" "$cache_read")

      # Build cost detail entries (input + output separately, pydantic-ai style)
      if [ "$cost" != "null" ]; then
        local_input_price=0 local_output_price=0
        case "${call_model:-}" in
          *opus*)   local_input_price="0.000015";  local_output_price="0.000075" ;;
          *sonnet*) local_input_price="0.000003";  local_output_price="0.000015" ;;
          *haiku*)  local_input_price="0.0000008"; local_output_price="0.000004" ;;
        esac
        input_cost=$(jq -n --argjson t "$raw_input" --argjson cc "$cache_creation" --argjson cr "$cache_read" --argjson p "$local_input_price" \
          '($t * $p) + ($cc * $p * 1.25) + ($cr * $p * 0.1)')
        output_cost=$(jq -n --argjson t "$output_tokens" --argjson p "$local_output_price" '$t * $p')

        cost_detail=$(jq -n -c \
          --arg model "$call_model" \
          --argjson input_cost "$input_cost" \
          --argjson output_cost "$output_cost" \
          '[
            {attributes: {"gen_ai.operation.name":"chat","gen_ai.provider.name":"anthropic","gen_ai.request.model":$model,"gen_ai.response.model":$model,"gen_ai.system":"anthropic","gen_ai.token.type":"input"}, total: $input_cost},
            {attributes: {"gen_ai.operation.name":"chat","gen_ai.provider.name":"anthropic","gen_ai.request.model":$model,"gen_ai.response.model":$model,"gen_ai.system":"anthropic","gen_ai.token.type":"output"}, total: $output_cost}
          ]')
        accumulated_cost_details=$(echo "$accumulated_cost_details" | jq -c ". + $cost_detail")
      fi

      # Messages for all_messages accumulation
      call_input_msgs=$(echo "$call" | jq -c '.input_messages')
      call_output_msgs=$(echo "$call" | jq -c '.output_messages')
      accumulated_messages=$(echo "$accumulated_messages" | jq -c ". + $call_input_msgs + $call_output_msgs")

      # Span timing -- use transcript timestamp if available, else now
      if [ -n "$call_timestamp" ]; then
        # Convert ISO timestamp to nanoseconds
        call_ns=$(python3 -c "
from datetime import datetime, timezone
ts = datetime.fromisoformat('${call_timestamp}'.replace('Z', '+00:00'))
print(int(ts.timestamp() * 1e9))
" 2>/dev/null || echo "$ts_nano")
      else
        call_ns="$ts_nano"
      fi

      # Build child span attributes
      span_id=$(random_span_id)
      span_name="chat ${call_model}"

      attrs="[$(make_attr "logfire.msg" "$span_name")"
      attrs="$attrs,$(make_attr "logfire.span_type" "span")"
      attrs="$attrs,$(make_attr "gen_ai.operation.name" "chat")"
      attrs="$attrs,$(make_attr "gen_ai.system" "anthropic")"
      attrs="$attrs,$(make_attr "gen_ai.request.model" "$call_model")"
      attrs="$attrs,$(make_attr "gen_ai.response.model" "$call_model")"
      attrs="$attrs,$(make_int_attr "gen_ai.usage.input_tokens" "$input_tokens")"
      attrs="$attrs,$(make_int_attr "gen_ai.usage.output_tokens" "$output_tokens")"

      # Complex attributes: input/output messages
      attrs="$attrs,$(make_complex_attr "gen_ai.input.messages" "$call_input_msgs")"
      attrs="$attrs,$(make_complex_attr "gen_ai.output.messages" "$call_output_msgs")"

      # finish_reasons array
      finish_reasons=$(jq -n -c --arg r "$call_stop_reason" '[$r]')
      attrs="$attrs,$(make_complex_attr "gen_ai.response.finish_reasons" "$finish_reasons")"

      # Cost
      [ "$cost" != "null" ] && attrs="$attrs,$(make_double_attr "operation.cost" "$cost")"

      # JSON schema for complex attributes
      json_schema='{"type":"object","properties":{"gen_ai.input.messages":{"type":"array"},"gen_ai.output.messages":{"type":"array"}}}'
      attrs="$attrs,$(make_complex_attr "logfire.json_schema" "$json_schema")"

      attrs="$attrs]"

      span_json=$(build_span "$trace_id" "$span_id" "$root_span_id" "$span_name" "$call_ns" "$call_ns" "$attrs")
      spans_json=$(echo "$spans_json" | jq -c ". + [$span_json]")
    done

    # Send all child spans in one payload
    if [ "$(echo "$spans_json" | jq 'length')" -gt 0 ]; then
      payload=$(build_otlp_envelope "$spans_json")
      send_otlp "$payload"
    fi

    # Accumulate into state file
    update_state jq -c \
      --argjson new_msgs "$accumulated_messages" \
      --argjson new_costs "$accumulated_cost_details" \
      --argjson add_input "$total_input" \
      --argjson add_output "$total_output" \
      --argjson add_cache_create "$total_cache_creation" \
      --argjson add_cache_read "$total_cache_read" \
      '
        .all_messages += $new_msgs |
        .cost_details += $new_costs |
        .usage.input_tokens += $add_input |
        .usage.output_tokens += $add_output |
        .usage.cache_creation_input_tokens += $add_cache_create |
        .usage.cache_read_input_tokens += $add_cache_read
      ' "$STATE_FILE"
    ;;

  SessionEnd)
    if [ ! -f "$STATE_FILE" ]; then
      log_diag "warn" "SessionEnd without state file, skipping root span"
      exit 0
    fi

    acquire_lock || exit 0
    trap 'release_lock' EXIT

    state=$(cat "$STATE_FILE")
    root_span_id=$(echo "$state" | jq -r '.root_span_id')
    state_parent_span_id=$(echo "$state" | jq -r '.parent_span_id // empty')
    start_time=$(echo "$state" | jq -r '.start_time')
    cwd=$(echo "$state" | jq -r '.cwd // empty')
    model=$(echo "$state" | jq -r '.model // empty')

    # Final transcript parse to capture any messages missed by the last Stop event
    state_tp=$(echo "$state" | jq -r '.transcript_path // empty')
    last_line=$(echo "$state" | jq -r '.last_line // 0')
    final_tp="${transcript_path:-$state_tp}"
    if [ -n "$final_tp" ] && [ -f "$final_tp" ]; then
      remaining_calls=$(parse_transcript_slice "$final_tp" "$last_line")
      num_remaining=$(echo "$remaining_calls" | jq 'length')
      if [ "$num_remaining" -gt 0 ]; then
        # Accumulate remaining messages and costs into state
        for i in $(seq 0 $((num_remaining - 1))); do
          call=$(echo "$remaining_calls" | jq -c ".[$i]")
          call_model=$(echo "$call" | jq -r '.model // empty')
          [ -z "$call_model" ] && call_model="$model"
          call_input_msgs=$(echo "$call" | jq -c '.input_messages')
          call_output_msgs=$(echo "$call" | jq -c '.output_messages')

          raw_input=$(echo "$call" | jq -r '.usage.input_tokens // 0')
          output_tokens=$(echo "$call" | jq -r '.usage.output_tokens // 0')
          cache_creation=$(echo "$call" | jq -r '.usage.cache_creation_input_tokens // 0')
          cache_read=$(echo "$call" | jq -r '.usage.cache_read_input_tokens // 0')
          input_tokens=$((raw_input + cache_creation + cache_read))

          cost=$(calculate_cost "$call_model" "$raw_input" "$output_tokens" "$cache_creation" "$cache_read")

          if [ "$cost" != "null" ]; then
            local_input_price=0 local_output_price=0
            case "${call_model:-}" in
              *opus*)   local_input_price="0.000015";  local_output_price="0.000075" ;;
              *sonnet*) local_input_price="0.000003";  local_output_price="0.000015" ;;
              *haiku*)  local_input_price="0.0000008"; local_output_price="0.000004" ;;
            esac
            input_cost=$(jq -n --argjson t "$raw_input" --argjson cc "$cache_creation" --argjson cr "$cache_read" --argjson p "$local_input_price" \
              '($t * $p) + ($cc * $p * 1.25) + ($cr * $p * 0.1)')
            output_cost=$(jq -n --argjson t "$output_tokens" --argjson p "$local_output_price" '$t * $p')

            cost_detail=$(jq -n -c \
              --arg model "$call_model" \
              --argjson input_cost "$input_cost" \
              --argjson output_cost "$output_cost" \
              '[
                {attributes: {"gen_ai.operation.name":"chat","gen_ai.provider.name":"anthropic","gen_ai.request.model":$model,"gen_ai.response.model":$model,"gen_ai.system":"anthropic","gen_ai.token.type":"input"}, total: $input_cost},
                {attributes: {"gen_ai.operation.name":"chat","gen_ai.provider.name":"anthropic","gen_ai.request.model":$model,"gen_ai.response.model":$model,"gen_ai.system":"anthropic","gen_ai.token.type":"output"}, total: $output_cost}
              ]')

            update_state jq -c \
              --argjson new_msgs "$(echo "[$call_input_msgs, $call_output_msgs]" | jq -c 'add')" \
              --argjson new_costs "$cost_detail" \
              --argjson add_input "$input_tokens" \
              --argjson add_output "$output_tokens" \
              --argjson add_cache_create "$cache_creation" \
              --argjson add_cache_read "$cache_read" \
              '
                .all_messages += $new_msgs |
                .cost_details += $new_costs |
                .usage.input_tokens += $add_input |
                .usage.output_tokens += $add_output |
                .usage.cache_creation_input_tokens += $add_cache_create |
                .usage.cache_read_input_tokens += $add_cache_read
              ' "$STATE_FILE"
          else
            update_state jq -c \
              --argjson new_msgs "$(echo "[$call_input_msgs, $call_output_msgs]" | jq -c 'add')" \
              '
                .all_messages += $new_msgs
              ' "$STATE_FILE"
          fi
        done

        # Also emit child spans for these remaining calls
        remaining_spans="[]"
        for i in $(seq 0 $((num_remaining - 1))); do
          call=$(echo "$remaining_calls" | jq -c ".[$i]")
          call_model=$(echo "$call" | jq -r '.model // empty')
          [ -z "$call_model" ] && call_model="$model"
          call_stop_reason=$(echo "$call" | jq -r '.stop_reason // "stop"')
          call_timestamp=$(echo "$call" | jq -r '.timestamp // empty')

          raw_input=$(echo "$call" | jq -r '.usage.input_tokens // 0')
          output_tokens=$(echo "$call" | jq -r '.usage.output_tokens // 0')
          cache_creation=$(echo "$call" | jq -r '.usage.cache_creation_input_tokens // 0')
          cache_read=$(echo "$call" | jq -r '.usage.cache_read_input_tokens // 0')
          input_tokens=$((raw_input + cache_creation + cache_read))
          cost=$(calculate_cost "$call_model" "$raw_input" "$output_tokens" "$cache_creation" "$cache_read")

          call_input_msgs=$(echo "$call" | jq -c '.input_messages')
          call_output_msgs=$(echo "$call" | jq -c '.output_messages')

          if [ -n "$call_timestamp" ]; then
            call_ns=$(python3 -c "
from datetime import datetime, timezone
ts = datetime.fromisoformat('${call_timestamp}'.replace('Z', '+00:00'))
print(int(ts.timestamp() * 1e9))
" 2>/dev/null || echo "$ts_nano")
          else
            call_ns="$ts_nano"
          fi

          span_id=$(random_span_id)
          span_name="chat ${call_model}"

          attrs="[$(make_attr "logfire.msg" "$span_name")"
          attrs="$attrs,$(make_attr "logfire.span_type" "span")"
          attrs="$attrs,$(make_attr "gen_ai.operation.name" "chat")"
          attrs="$attrs,$(make_attr "gen_ai.system" "anthropic")"
          attrs="$attrs,$(make_attr "gen_ai.request.model" "$call_model")"
          attrs="$attrs,$(make_attr "gen_ai.response.model" "$call_model")"
          attrs="$attrs,$(make_int_attr "gen_ai.usage.input_tokens" "$input_tokens")"
          attrs="$attrs,$(make_int_attr "gen_ai.usage.output_tokens" "$output_tokens")"
          attrs="$attrs,$(make_complex_attr "gen_ai.input.messages" "$call_input_msgs")"
          attrs="$attrs,$(make_complex_attr "gen_ai.output.messages" "$call_output_msgs")"
          finish_reasons=$(jq -n -c --arg r "$call_stop_reason" '[$r]')
          attrs="$attrs,$(make_complex_attr "gen_ai.response.finish_reasons" "$finish_reasons")"
          [ "$cost" != "null" ] && attrs="$attrs,$(make_double_attr "operation.cost" "$cost")"
          json_schema='{"type":"object","properties":{"gen_ai.input.messages":{"type":"array"},"gen_ai.output.messages":{"type":"array"}}}'
          attrs="$attrs,$(make_complex_attr "logfire.json_schema" "$json_schema")"
          attrs="$attrs]"

          span_json=$(build_span "$trace_id" "$span_id" "$root_span_id" "$span_name" "$call_ns" "$call_ns" "$attrs")
          remaining_spans=$(echo "$remaining_spans" | jq -c ". + [$span_json]")
        done

        if [ "$(echo "$remaining_spans" | jq 'length')" -gt 0 ]; then
          payload=$(build_otlp_envelope "$remaining_spans")
          send_otlp "$payload"
        fi

        # Re-read state after updates
        state=$(cat "$STATE_FILE")
      fi
    fi

    all_messages=$(echo "$state" | jq -c '.all_messages // []')

    # Extract final_result: last text part from last assistant message
    final_result=$(echo "$all_messages" | jq -c '
      [.[] | select(.role == "assistant")] | last |
      if . == null then null
      else [.parts[]? | select(.type == "text") | .content] | last // null
      end
    ' 2>/dev/null || echo 'null')

    # Build root span attributes (no token/cost — Logfire sums from child spans)
    attrs="[$(make_attr "logfire.msg" "Claude Code session")"
    attrs="$attrs,$(make_attr "logfire.span_type" "span")"
    attrs="$attrs,$(make_attr "agent_name" "claude-code")"
    attrs="$attrs,$(make_attr "gen_ai.agent.name" "claude-code")"
    attrs="$attrs,$(make_attr "gen_ai.system" "anthropic")"
    [ -n "$model" ] && attrs="$attrs,$(make_attr "gen_ai.response.model" "$model")"
    [ -n "$model" ] && attrs="$attrs,$(make_attr "model_name" "$model")"
    attrs="$attrs,$(make_attr "session.id" "$session_id")"
    [ -n "$cwd" ] && attrs="$attrs,$(make_attr "session.cwd" "$cwd")"

    # Complex attributes
    if [ "$final_result" != "null" ]; then
      attrs="$attrs,$(make_complex_attr "final_result" "$final_result")"
    fi
    attrs="$attrs,$(make_complex_attr "pydantic_ai.all_messages" "$all_messages")"

    # JSON schema
    json_schema='{"type":"object","properties":{"final_result":{"type":"object"},"pydantic_ai.all_messages":{"type":"array"}}}'
    attrs="$attrs,$(make_complex_attr "logfire.json_schema" "$json_schema")"

    attrs="$attrs]"

    span_json=$(build_span "$trace_id" "$root_span_id" "$state_parent_span_id" "Claude Code session" "$start_time" "$ts_nano" "$attrs")
    payload=$(build_otlp_envelope "[$span_json]")
    send_otlp "$payload"

    rm -f "$STATE_FILE"
    ;;
esac
