"""Step Functions helpers — drive and inspect workflow executions.

Lets tests start an execution with a known input, wait for it to finish, and inspect
BOTH the result and the execution *history* (the recorded sequence of states). The
history is what makes "test a Lambda workflow" possible: you can assert which path the
workflow took (approve vs decline), that retries happened, and that an error routed to
the Catch fallback — every branch becomes an assertable fact.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .aws import client


def start_execution(state_machine_arn: str, input_payload: dict[str, Any]) -> str:
    """Start a Step Functions execution and return its execution ARN."""
    response = client("stepfunctions").start_execution(
        stateMachineArn=state_machine_arn, input=json.dumps(input_payload)
    )
    return response["executionArn"]


def wait_for_execution(execution_arn: str, timeout_seconds: float = 120.0) -> dict[str, Any]:
    """Poll an execution until it leaves RUNNING, then return its describe-execution dict.

    Raises:
        AssertionError: if it doesn't finish within the timeout.
    """
    sfn = client("stepfunctions")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        description = sfn.describe_execution(executionArn=execution_arn)
        if description["status"] != "RUNNING":
            return description
        time.sleep(3)
    raise AssertionError(f"Execution {execution_arn} did not finish in {timeout_seconds}s")


def get_visited_states(execution_arn: str) -> list[str]:
    """Return the ordered list of state names the execution entered.

    Built from the execution history's StateEntered events — this is the workflow's
    actual path, which tests assert against (e.g. did it go through PrepareDecline?).
    """
    sfn = client("stepfunctions")
    visited: list[str] = []
    params: dict[str, Any] = {"executionArn": execution_arn, "maxResults": 1000}
    while True:
        history = sfn.get_execution_history(**params)
        for event in history["events"]:
            details = event.get("stateEnteredEventDetails")
            if details:
                visited.append(details["name"])
        if "nextToken" not in history:
            break
        params["nextToken"] = history["nextToken"]
    return visited


def get_output(description: dict[str, Any]) -> dict[str, Any]:
    """Parse the execution output JSON from a describe-execution dict."""
    return json.loads(description.get("output", "{}"))
