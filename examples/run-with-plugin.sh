#!/usr/bin/env bash
#
# Launch Claude Code with the logfire plugin loaded from this repo.
#
# Usage:
#   ./examples/run-with-plugin.sh
#
# Reads LOGFIRE_TOKEN (and optionally LOGFIRE_BASE_URL) from .env if present.

set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "$0")/.." && pwd)"

# Source .env if it exists
if [ -f "$PLUGIN_DIR/.env" ]; then
  set -a
  . "$PLUGIN_DIR/.env"
  set +a
fi

exec claude --plugin-dir "$PLUGIN_DIR"
