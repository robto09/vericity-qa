"""Polling / eventual-consistency helpers — the backbone of async testing.

This is the single most important utility in the framework. The system is
**event-driven and asynchronous**: when you submit an application, the result
(underwriting decision, issued document, billing row) appears *seconds later*, after
EventBridge → Step Functions → more Lambdas → SQS → RDS all run.

A naive test that checks the result immediately will fail intermittently — the classic
"flaky test." The fix is NOT `time.sleep(10)` everywhere (slow and still flaky); it's
**poll until a condition is true, with a timeout**. That's what `wait_until` does, and
nearly every async/event/workflow test below builds on it.

This pattern — *retry an assertion under eventual consistency* — is exactly what you'd
describe in an interview when asked "how do you test an asynchronous process?"
"""
from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class WaitTimeout(AssertionError):
    """Raised when a polled condition never became true within the timeout.

    Subclasses AssertionError so a timeout reads as a test FAILURE (the expected
    thing didn't happen in time), not an unexpected error.
    """


def wait_until(
    condition: Callable[[], T],
    *,
    timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 3.0,
    description: str = "condition",
) -> T:
    """Poll ``condition`` until it returns a truthy value, or fail after ``timeout``.

    Args:
        condition: a zero-arg callable. Returns a truthy value when satisfied (that
            value is returned to the caller, so you can both wait AND capture the
            result in one call). Returning falsy/None means "not yet, keep polling."
        timeout_seconds: max total time to wait before failing.
        poll_interval_seconds: how long to sleep between attempts.
        description: human-readable label used in the timeout message.

    Returns:
        The first truthy value ``condition`` returns.

    Raises:
        WaitTimeout: if the condition never becomes truthy within the timeout.

    Why poll instead of one long sleep: polling returns as SOON as the condition is
    met (fast on the happy path) and only burns the full timeout on genuine failure.
    """
    deadline = time.monotonic() + timeout_seconds
    last_exception: Exception | None = None

    while time.monotonic() < deadline:
        try:
            result = condition()
            if result:
                return result
        except Exception as exception:  # noqa: BLE001
            # A transient error mid-poll (e.g. a row not created yet) shouldn't abort
            # the wait — remember it and retry until the deadline.
            last_exception = exception
        time.sleep(poll_interval_seconds)

    # One final attempt right at the deadline, so a just-in-time success isn't missed.
    try:
        result = condition()
        if result:
            return result
    except Exception as exception:  # noqa: BLE001
        last_exception = exception

    message = f"Timed out after {timeout_seconds}s waiting for: {description}"
    if last_exception is not None:
        message += f" (last error: {type(last_exception).__name__}: {last_exception})"
    raise WaitTimeout(message)


def stays_true(
    condition: Callable[[], bool],
    *,
    duration_seconds: float = 10.0,
    poll_interval_seconds: float = 2.0,
    description: str = "condition",
) -> None:
    """Assert a condition STAYS true for a duration (the opposite of wait_until).

    Used for negative async assertions like "no duplicate row ever appears" or "the
    message does NOT reach the DLQ" — you can't prove a negative instantly in an async
    system, so you verify it holds over a window.

    Raises:
        AssertionError: the moment the condition becomes false within the window.
    """
    deadline = time.monotonic() + duration_seconds
    while time.monotonic() < deadline:
        if not condition():
            raise AssertionError(f"Condition stopped being true: {description}")
        time.sleep(poll_interval_seconds)
