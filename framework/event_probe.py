"""EventBridge probe — capture published events so tests can assert on them.

THE problem with validating EventBridge: events are fire-and-forget. A producer puts an
event on the bus and moves on; there's no built-in way to "read the last events." So to
test "was the event published, with the right shape?", you must **subscribe a test
consumer** before triggering the producer, then read what it captured.

THE technique (standard for EventBridge QA):
  1. Create a temporary SQS queue (the "probe").
  2. Add a temporary EventBridge rule that routes the detail-types we care about,
     from the platform's bus, to that probe queue.
  3. Trigger the producer (e.g. submit an application).
  4. Poll the probe queue and collect the captured events.
  5. Assert on them: count, source, detail-type, detail schema, ordering.
  6. Tear down the rule + queue.

This `EventProbe` context manager wraps all of that so a test reads cleanly:

    with EventProbe(["ApplicationSubmitted"]) as probe:
        policy_id = api.create_application()
        events = probe.wait_for_events(count=1)
        assert events[0]["detail-type"] == "ApplicationSubmitted"

It directly answers: "events published correctly", "event schema correct", "received by
consumers", "duplicate events", "missing events", "out-of-order events".
"""
from __future__ import annotations

import json
import time
from typing import Any

from .aws import client
from .config import get_config


class EventProbe:
    """Context manager that captures EventBridge events into a temporary SQS queue."""

    def __init__(self, detail_types: list[str], name_suffix: str = "") -> None:
        """
        Args:
            detail_types: which event detail-types to capture (e.g. ["PolicyApproved"]).
            name_suffix: optional unique suffix so parallel tests don't collide.
        """
        self.detail_types = detail_types
        # A unique name per probe instance so concurrent tests get isolated queues.
        # (Uses a monotonic counter via id() + time to avoid Math.random-style issues.)
        unique = f"{int(time.monotonic() * 1000)}{name_suffix}"
        self.queue_name = f"qa-probe-{unique}"
        self.rule_name = f"qa-probe-rule-{unique}"
        self.target_id = f"qa-probe-target-{unique}"

        self._sqs = client("sqs")
        self._events = client("events")
        self._sts = client("sts")
        self._config = get_config()

        self.queue_url: str = ""
        self.queue_arn: str = ""

    def __enter__(self) -> "EventProbe":
        # 1. Create the probe SQS queue.
        self.queue_url = self._sqs.create_queue(QueueName=self.queue_name)["QueueUrl"]
        self.queue_arn = self._sqs.get_queue_attributes(
            QueueUrl=self.queue_url, AttributeNames=["QueueArn"]
        )["Attributes"]["QueueArn"]

        # 2. Allow EventBridge to send messages to this queue (resource policy).
        self._sqs.set_queue_attributes(
            QueueUrl=self.queue_url,
            Attributes={
                "Policy": json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Principal": {"Service": "events.amazonaws.com"},
                                "Action": "sqs:SendMessage",
                                "Resource": self.queue_arn,
                            }
                        ],
                    }
                )
            },
        )

        # 3. Create the rule on the platform's bus matching our detail-types.
        self._events.put_rule(
            Name=self.rule_name,
            EventBusName=self._config.event_bus_name,
            EventPattern=json.dumps(
                {"source": ["vericity.policy"], "detail-type": self.detail_types}
            ),
            State="ENABLED",
        )

        # 4. Point the rule at the probe queue.
        self._events.put_targets(
            Rule=self.rule_name,
            EventBusName=self._config.event_bus_name,
            Targets=[{"Id": self.target_id, "Arn": self.queue_arn}],
        )

        # EventBridge rule propagation is not instant and the delay is variable, so a
        # fixed sleep is unreliable (it caused flaky "captured 0 events" failures). We
        # instead ACTIVELY VERIFY the rule->queue path is live before returning: publish
        # a synthetic probe event matching one of our detail-types and poll until it
        # arrives. Only then is it safe for the test to trigger the real producer.
        self._wait_until_rule_is_live()
        return self

    def _wait_until_rule_is_live(self, timeout_seconds: float = 30.0) -> None:
        """Confirm the temporary rule actually routes to the probe queue, deterministically.

        Publishes a marker event carrying a unique probe_marker, then drains the queue
        until that marker appears. This proves the rule is active — eliminating the
        race between rule creation and the producer firing.
        """
        marker = f"probe-ready-{self.queue_name}"
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            # Re-publish each loop: the first puts may be dropped while the rule is
            # still propagating, so we keep nudging until one lands.
            self._events.put_events(
                Entries=[
                    {
                        "Source": "vericity.policy",
                        "DetailType": self.detail_types[0],
                        "Detail": json.dumps({"probe_marker": marker, "policy_id": marker}),
                        "EventBusName": self._config.event_bus_name,
                    }
                ]
            )
            time.sleep(2)
            for event in self._drain_once():
                detail = event.get("detail", {})
                if isinstance(detail, str):
                    detail = json.loads(detail)
                if detail.get("probe_marker") == marker:
                    return  # rule is live and routing to our queue
        raise RuntimeError(
            f"EventProbe rule never became active within {timeout_seconds}s "
            f"(detail_types={self.detail_types})"
        )

    def __exit__(self, *exc: Any) -> None:
        # Tear down in reverse order, tolerating partial setup on error.
        try:
            self._events.remove_targets(
                Rule=self.rule_name,
                EventBusName=self._config.event_bus_name,
                Ids=[self.target_id],
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            self._events.delete_rule(
                Name=self.rule_name, EventBusName=self._config.event_bus_name
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            self._sqs.delete_queue(QueueUrl=self.queue_url)
        except Exception:  # noqa: BLE001
            pass

    def _drain_once(self) -> list[dict[str, Any]]:
        """Read whatever events are currently in the probe queue (one batch poll)."""
        captured: list[dict[str, Any]] = []
        response = self._sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=2,  # short long-poll
        )
        for message in response.get("Messages", []):
            # EventBridge delivers the full event envelope as the SQS message body.
            captured.append(json.loads(message["Body"]))
            # Delete so we don't re-read it on the next poll.
            self._sqs.delete_message(
                QueueUrl=self.queue_url, ReceiptHandle=message["ReceiptHandle"]
            )
        return captured

    def collect(self, timeout_seconds: float = 45.0) -> list[dict[str, Any]]:
        """Drain the probe queue repeatedly until the timeout, returning ALL events.

        Used when you want to see everything that arrived in a window (e.g. to assert
        NO duplicates, or to inspect ordering).
        """
        collected: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            collected.extend(self._drain_once())
            time.sleep(1)
        return collected

    def wait_for_events(
        self, count: int = 1, timeout_seconds: float = 45.0
    ) -> list[dict[str, Any]]:
        """Wait until at least ``count`` events are captured, then return them.

        Raises:
            AssertionError: if fewer than ``count`` events arrive within the timeout
                (a "missing event" failure — the producer didn't publish as expected).
        """
        collected: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            collected.extend(self._drain_once())
            if len(collected) >= count:
                return collected
            time.sleep(1)
        raise AssertionError(
            f"Expected >= {count} '{self.detail_types}' event(s) within "
            f"{timeout_seconds}s, captured {len(collected)}. Possible MISSING event."
        )
