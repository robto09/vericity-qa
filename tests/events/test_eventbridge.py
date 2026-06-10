"""EventBridge tests — validate events are published correctly.

These use the EventProbe pattern (framework/event_probe.py): subscribe a temporary SQS
queue to the bus BEFORE triggering the producer, then read back the captured events.

Covers every event-validation concern from the QA requirements:
  - event published correctly        -> test_application_submitted_event_is_published
  - event schema correct             -> test_*_event_schema
  - received by consumers            -> (the probe IS a consumer; capture proves delivery)
  - duplicate events                 -> test_no_duplicate_events
  - missing events                   -> probe.wait_for_events raises on timeout
  - out-of-order events              -> test_lifecycle_events_in_causal_order

Answers the interview question: "How would you validate events published to EventBridge?"

These are slow (async propagation + probe setup), so marked accordingly.
"""
from __future__ import annotations

import json

import pytest
from jsonschema import validate

from framework.event_probe import EventProbe
from framework.schemas import (
    APPLICATION_SUBMITTED_DETAIL_SCHEMA,
    POLICY_APPROVED_DETAIL_SCHEMA,
)
from framework.wait import wait_until

pytestmark = [pytest.mark.events, pytest.mark.slow]


def _detail(event: dict) -> dict:
    """Extract the `detail` payload from a captured EventBridge envelope."""
    detail = event["detail"]
    return json.loads(detail) if isinstance(detail, str) else detail


class TestApplicationSubmittedEvent:
    """Submitting an application publishes a correct ApplicationSubmitted event."""

    def test_application_submitted_event_is_published(self, api):
        # Subscribe FIRST, then trigger — otherwise we'd miss the event.
        with EventProbe(["ApplicationSubmitted"]) as probe:
            policy_id = api.create_application()
            events = probe.wait_for_events(count=1)

        # Exactly our policy's event should be present (published correctly).
        matching = [e for e in events if _detail(e).get("policy_id") == policy_id]
        assert matching, f"No ApplicationSubmitted event for {policy_id} (MISSING event)"

    def test_application_submitted_event_envelope(self, api):
        with EventProbe(["ApplicationSubmitted"]) as probe:
            policy_id = api.create_application()
            events = probe.wait_for_events(count=1)

        event = next(e for e in events if _detail(e).get("policy_id") == policy_id)
        # The EventBridge envelope fields the platform contract guarantees.
        assert event["source"] == "vericity.policy"
        assert event["detail-type"] == "ApplicationSubmitted"

    def test_application_submitted_event_schema(self, api):
        with EventProbe(["ApplicationSubmitted"]) as probe:
            policy_id = api.create_application()
            events = probe.wait_for_events(count=1)

        detail = _detail(next(e for e in events if _detail(e).get("policy_id") == policy_id))
        # Contract test on the detail payload — missing/wrong-typed fields fail here.
        validate(instance=detail, schema=APPLICATION_SUBMITTED_DETAIL_SCHEMA)

    def test_no_duplicate_events(self, api):
        # One application must yield exactly ONE ApplicationSubmitted event. Collect
        # over a window (not just first match) to catch accidental duplicates.
        with EventProbe(["ApplicationSubmitted"]) as probe:
            policy_id = api.create_application()
            events = probe.collect(timeout_seconds=30)

        mine = [e for e in events if _detail(e).get("policy_id") == policy_id]
        assert len(mine) == 1, f"Expected 1 event, got {len(mine)} (DUPLICATE event)"


class TestPolicyApprovedEvent:
    """An approvable application eventually publishes a correct PolicyApproved event."""

    def test_policy_approved_event_published_and_valid(self, api):
        with EventProbe(["PolicyApproved"]) as probe:
            # A young, healthy, non-tobacco applicant approves (low risk score).
            policy_id = api.create_application(applicant_age=30, tobacco_use=False)
            # Underwriting runs async — give it generous time to reach the decision.
            events = probe.wait_for_events(count=1, timeout_seconds=90)

        approved = [e for e in events if _detail(e).get("policy_id") == policy_id]
        assert approved, f"No PolicyApproved event for {policy_id}"
        detail = _detail(approved[0])
        validate(instance=detail, schema=POLICY_APPROVED_DETAIL_SCHEMA)
        # Business rule: a healthy young applicant should land in a non-decline tier.
        assert detail["risk_tier"] in {"PREFERRED", "STANDARD", "SUBSTANDARD"}


class TestEventOrdering:
    """Lifecycle events occur in causal order across the whole flow."""

    def test_lifecycle_events_in_causal_order(self, api):
        # Capture all three lifecycle events for one policy and assert the causal
        # order: an application is submitted BEFORE it's approved BEFORE it's issued.
        # (EventBridge doesn't guarantee global ordering, but these are causally
        #  sequential — each triggers the next — so their arrival order must hold.)
        with EventProbe(["ApplicationSubmitted", "PolicyApproved", "PolicyIssued"]) as probe:
            policy_id = api.create_application(applicant_age=30, tobacco_use=False)
            events = probe.collect(timeout_seconds=120)

        # Build an ordered list of detail-types for OUR policy only. The envelope
        # `time` field has ONE-SECOND granularity, so causally-adjacent events can tie;
        # Python's sort is stable, so ties keep their queue-arrival order (the `events`
        # list is in arrival order) — that tie-break is what keeps this deterministic.
        mine = [
            (e["detail-type"], e.get("time", ""))
            for e in events
            if _detail(e).get("policy_id") == policy_id
        ]
        types_in_order = [t for t, _ in sorted(mine, key=lambda x: x[1])]

        # All three should have fired, in this causal sequence.
        assert types_in_order == ["ApplicationSubmitted", "PolicyApproved", "PolicyIssued"], (
            f"Out-of-order or missing lifecycle events: {types_in_order}"
        )
