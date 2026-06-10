"""SQS + DLQ tests — the asynchronous billing pipeline.

Answers two interview questions:
  - "How would you test an SQS-based asynchronous process?"
  - "How would you test retries and dead-letter queues (DLQs)?"

Wiring under test: EventBridge(PolicyIssued) -> SQS billing queue -> billing Lambda ->
RDS, with a redrive policy sending failed messages to a DLQ after 3 receives.

We test by sending messages DIRECTLY to the billing queue (bypassing the upstream flow)
so we control the payload precisely — a valid PolicyIssued shape for the happy path, and
a deliberately-malformed one to force the failure -> retry -> DLQ path.

These are slow (async processing + DLQ requires the message to be received 3x, each
governed by the 60s visibility timeout, so DLQ landing can take a couple of minutes).
"""
from __future__ import annotations

import pytest

from framework.config import get_config
from framework.data import get_policy_item
from framework.models import generate_test_policy_id
from framework.sqs import receive_all, send_message
from framework.wait import stays_true, wait_until

pytestmark = [pytest.mark.async_, pytest.mark.slow]


def _policy_issued_message(policy_id: str, monthly_premium: str = "50.00") -> dict:
    """A well-formed PolicyIssued message body as EventBridge would deliver it to SQS."""
    return {
        "detail-type": "PolicyIssued",
        "source": "vericity.policy",
        "detail": {
            "policy_id": policy_id,
            "final_monthly_premium": monthly_premium,
            "document_s3_key": f"policies/{policy_id}/policy.txt",
        },
    }


class TestAsyncBillingHappyPath:
    """A valid billing message is processed asynchronously and flips the policy ACTIVE."""

    def test_billing_message_marks_policy_active(self, seeded_policy):
        # Seed an ISSUED policy (billing transitions ISSUED -> ACTIVE), then drop a
        # PolicyIssued message on the billing queue.
        policy_id = seeded_policy(status="ISSUED", final_monthly_premium="50.00")
        config = get_config()

        send_message(config.billing_queue_url, _policy_issued_message(policy_id, "50.00"))

        # Poll until billing (async) processes it and the policy reaches ACTIVE.
        wait_until(
            lambda: (get_policy_item(policy_id) or {}).get("status") == "ACTIVE",
            timeout_seconds=90,
            description=f"policy {policy_id} to become ACTIVE via async billing",
        )


class TestIdempotency:
    """At-least-once delivery: the SAME message twice must not double-bill."""

    def test_duplicate_message_does_not_double_process(self, seeded_policy):
        # The billing Lambda + RDS unique constraint guard against duplicate ledger
        # rows. We can't easily read RDS from here, so we assert the OBSERVABLE
        # invariant: sending the same PolicyIssued twice still ends in exactly ACTIVE
        # (no error state, no regression) — and rely on the platform's ON CONFLICT
        # guard (unit-tested in the platform repo) for the ledger-row uniqueness.
        policy_id = seeded_policy(status="ISSUED", final_monthly_premium="75.00")
        config = get_config()

        message = _policy_issued_message(policy_id, "75.00")
        send_message(config.billing_queue_url, message)
        send_message(config.billing_queue_url, message)  # duplicate

        wait_until(
            lambda: (get_policy_item(policy_id) or {}).get("status") == "ACTIVE",
            timeout_seconds=90,
            description="policy ACTIVE after duplicate messages",
        )
        # And it STAYS active (didn't flip to an error/regressed state from the dup).
        stays_true(
            lambda: (get_policy_item(policy_id) or {}).get("status") == "ACTIVE",
            duration_seconds=8,
            description="policy stays ACTIVE after duplicate processing",
        )


class TestRetryAndDLQ:
    """A message that always fails is retried and lands in the DLQ — not lost, not looping."""

    def test_poison_message_lands_in_dlq(self):
        config = get_config()
        # A poison message: references a policy_id that doesn't exist in DynamoDB AND
        # is malformed enough that billing raises -> SQS retries -> after 3 receives,
        # the redrive policy moves it to the DLQ.
        poison_policy_id = generate_test_policy_id()
        poison = {
            "detail-type": "PolicyIssued",
            "source": "vericity.policy",
            # Missing 'detail' entirely -> billing handler raises on access -> retry.
            "garbage": poison_policy_id,
        }

        send_message(config.billing_queue_url, poison)

        # Wait for it to appear in the DLQ. maxReceiveCount=3 with a 60s visibility
        # timeout means up to ~3 minutes before it gives up to the DLQ.
        def _poison_in_dlq() -> bool:
            bodies = receive_all(config.billing_dlq_url)
            return any(
                (b.get("garbage") == poison_policy_id) for b in bodies
            )

        wait_until(
            _poison_in_dlq,
            timeout_seconds=240,
            poll_interval_seconds=10,
            description="poison message to land in the DLQ after retries",
        )
