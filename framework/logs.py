"""CloudWatch Logs helpers — observability is testable.

In a distributed system you often can't watch an async Lambda's internals directly; its
**structured logs are the observability surface**. These helpers let tests assert that a
Lambda logged the expected event (e.g. billing logged "First premium recorded"), and to
pull a logged JSON payload (e.g. the migration report) back out for assertions.

This is the "Monitoring and log verification (CloudWatch)" testing type.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .aws import client


def _log_group(function_name: str) -> str:
    """CloudWatch log group name for a Lambda function."""
    return f"/aws/lambda/{function_name}"


def find_log_events(
    function_name: str,
    filter_pattern: str,
    *,
    since_seconds: int = 600,
    timeout_seconds: float = 90.0,
    poll_interval_seconds: float = 5.0,
) -> list[str]:
    """Poll a Lambda's log group for events matching a substring, until found or timeout.

    Logs are themselves eventually-consistent (they take a few seconds to appear after
    the Lambda runs), so this polls rather than reading once.

    Args:
        function_name: the Lambda whose logs to search.
        filter_pattern: a CloudWatch Logs filter pattern (use a quoted substring like
            '"First premium recorded"').
        since_seconds: how far back to search.

    Returns:
        The matching log message strings (empty list if none within the timeout).
    """
    logs = client("logs")
    start_time_ms = int((time.time() - since_seconds) * 1000)
    deadline = time.monotonic() + timeout_seconds

    while time.monotonic() < deadline:
        try:
            response = logs.filter_log_events(
                logGroupName=_log_group(function_name),
                startTime=start_time_ms,
                filterPattern=filter_pattern,
                limit=50,
            )
            messages = [event["message"] for event in response.get("events", [])]
            if messages:
                return messages
        except logs.exceptions.ResourceNotFoundException:
            # Log group doesn't exist yet (function never invoked) — keep waiting.
            pass
        time.sleep(poll_interval_seconds)

    return []


def extract_json_context(log_message: str) -> dict[str, Any] | None:
    """Parse a structured JSON log line and return its `context` block (or None).

    The platform logs single-line JSON like {"level":..,"message":..,"context":{..}}.
    Lambda may prefix the line with a timestamp/request id, so we find the JSON object.
    """
    brace_index = log_message.find("{")
    if brace_index == -1:
        return None
    try:
        parsed = json.loads(log_message[brace_index:])
    except json.JSONDecodeError:
        return None
    return parsed.get("context")
