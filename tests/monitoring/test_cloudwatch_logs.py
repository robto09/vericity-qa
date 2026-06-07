"""CloudWatch log / monitoring verification tests.

The "Monitoring and log verification (CloudWatch)" testing type.

In an async, distributed system you frequently can't observe a step directly — the
structured logs are the observability surface. These tests prove the system is
*observable*: after triggering a flow, the relevant Lambdas logged the expected
structured events, with the right context, to CloudWatch. If you can't assert on logs,
you can't prove (or debug) what happened inside an async Lambda in production.

Slow: triggers a flow and waits for log propagation.
"""
from __future__ import annotations

import pytest

from framework.data import get_policy_item
from framework.logs import extract_json_context, find_log_events
from framework.wait import wait_until

pytestmark = [pytest.mark.monitoring, pytest.mark.slow]


class TestUnderwritingObservability:
    def test_risk_score_is_logged(self, api):
        # Trigger a full application and wait for underwriting to finish.
        policy_id = api.create_application(applicant_age=30, tobacco_use=False)
        wait_until(
            lambda: (get_policy_item(policy_id) or {}).get("status")
            in {"APPROVED", "ISSUED", "ACTIVE"},
            timeout_seconds=90,
            description="underwriting to complete",
        )

        # The risk_score Lambda must have logged the computed score for this policy —
        # proving the underwriting step is observable.
        messages = find_log_events(
            "vericity-uw-risk-score-dev",
            f'"{policy_id}"',
            since_seconds=300,
            timeout_seconds=60,
        )
        assert messages, f"No risk-score log for {policy_id} — underwriting not observable"
        contexts = [extract_json_context(m) for m in messages]
        scored = [c for c in contexts if c and c.get("policy_id") == policy_id and "risk_score" in c]
        assert scored, "risk_score not present in the logged context"
        assert 0 <= scored[0]["risk_score"] <= 100


class TestBillingObservability:
    def test_first_premium_recorded_is_logged(self, api):
        # Run a full lifecycle to ACTIVE, which means billing ran.
        policy_id = api.create_application(applicant_age=30, tobacco_use=False)
        wait_until(
            lambda: (get_policy_item(policy_id) or {}).get("status") == "ACTIVE",
            timeout_seconds=120,
            description="policy to reach ACTIVE (billing ran)",
        )

        # Billing must have logged "First premium recorded" for THIS policy — this is
        # how we confirm (without direct RDS access) that billing wrote to the ledger.
        # Logs are themselves eventually-consistent (they lag the actual write by a few
        # seconds), and we need OUR policy's line specifically, so we poll until it
        # appears rather than reading once.
        def _our_billing_log():
            messages = find_log_events(
                "vericity-billing-dev",
                '"First premium recorded"',
                since_seconds=600,
                timeout_seconds=10,
            )
            for message in messages:
                context = extract_json_context(message)
                if context and context.get("policy_id") == policy_id:
                    return context
            return None

        ours = wait_until(
            _our_billing_log,
            timeout_seconds=90,
            poll_interval_seconds=8,
            description=f"billing log for {policy_id}",
        )
        # The logged amount should be present (the premium it billed).
        assert "amount_due" in ours
