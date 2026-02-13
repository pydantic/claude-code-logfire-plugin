# /// script
# requires-python = ">=3.10"
# dependencies = ["logfire", "python-dotenv"]
# ///
"""
Distributed tracing example: Python orchestrator -> Claude Code session.

Starts a Logfire trace, then launches Claude Code with TRACEPARENT so the
Claude Code session appears as a child span in your Logfire project.

Usage:
    export LOGFIRE_TOKEN=your-logfire-write-token
    uv run examples/distributed-tracing.py "Explain what TRACEPARENT is in one sentence"
"""

import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

import logfire
from opentelemetry import trace
from opentelemetry.context import get_current
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logfire.configure(service_name="orchestrator")

PLUGIN_DIR = str(Path(__file__).resolve().parent.parent)


def build_traceparent() -> str:
    """Extract W3C TRACEPARENT from the current OTel context."""
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier, context=get_current())
    return carrier["traceparent"]


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else "Say hello"

    with logfire.span("orchestrate claude-code", prompt=prompt):
        traceparent = build_traceparent()
        print(f"TRACEPARENT: {traceparent}")

        env = os.environ.copy()
        env["TRACEPARENT"] = traceparent
        env.pop("CLAUDECODE", None)

        result = subprocess.run(
            ["claude", "--print", "--plugin-dir", PLUGIN_DIR, "--", prompt],
            env=env,
        )
        if result.returncode != 0:
            print(f"claude exited with code {result.returncode}", file=sys.stderr)

    trace.get_tracer_provider().force_flush()  # type: ignore[union-attr]
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
