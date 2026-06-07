"""Step Functions workflow tests — the underwriting state machine.

Answers: "How would you test an AWS Lambda workflow?"

We drive the REAL state machine with known inputs (mimicking the ApplicationSubmitted
event envelope) and assert on the execution outcome AND the path it took through the
states. Because the workflow is deterministic (risk scoring is pure), specific inputs
land on specific branches, which we can assert exactly.

Coverage:
  - Approve path: a low-risk applicant takes the approve branch and ends APPROVED.
  - Decline path: a high-risk applicant takes the decline branch and ends DECLINED.
  - Parallel + merge: the 3 concurrent data checks run and merge (visited states).
  - Error fallback (Catch): a malformed input routes to ManualReview, NOT a failed
    execution — proving the safety net.
  - State path assertions via execution history.

These start executions directly (not via the API) so each test controls the exact input
and isolates the workflow from the rest of the system. They're slow (real async).
"""
from __future__ import annotations

import pytest

from framework.config import get_config
from framework.models import generate_test_policy_id
from framework.stepfunctions import (
    get_output,
    get_visited_states,
    start_execution,
    wait_for_execution,
)

pytestmark = [pytest.mark.workflow, pytest.mark.slow]


def _event_input(policy_id: str, age: int, tobacco: bool) -> dict:
    """Build an input mimicking the EventBridge-wrapped ApplicationSubmitted event.

    The state machine's first state unwraps `$.detail`, so we wrap our payload the same
    way EventBridge would — this tests the workflow exactly as it runs in production.
    """
    return {
        "detail": {
            "policy_id": policy_id,
            "applicant_age": age,
            "coverage_amount": 250_000,
            "term_years": 20,
            "tobacco_use": tobacco,
        }
    }


@pytest.fixture(scope="module")
def sm_arn():
    return get_config().underwriting_state_machine_arn


class TestApprovePath:
    """A low-risk applicant flows through to APPROVED."""

    def test_low_risk_applicant_is_approved(self, sm_arn, seeded_policy):
        # seeded_policy creates a real DynamoDB row so the decision step can update it.
        policy_id = seeded_policy(age=30, tobacco=False)
        arn = start_execution(sm_arn, _event_input(policy_id, age=30, tobacco=False))
        description = wait_for_execution(arn)

        assert description["status"] == "SUCCEEDED"
        assert get_output(description).get("final_status") == "APPROVED"

    def test_approve_path_visits_expected_states(self, sm_arn, seeded_policy):
        policy_id = seeded_policy(age=30, tobacco=False)
        arn = start_execution(sm_arn, _event_input(policy_id, age=30, tobacco=False))
        wait_for_execution(arn)

        visited = get_visited_states(arn)
        # The parallel-checks + merge + score + approve branch must all appear.
        assert "GatherDataInParallel" in visited
        assert "MergeChecks" in visited
        assert "RiskScore" in visited
        assert "PrepareApprove" in visited       # approve branch chosen
        assert "PrepareDecline" not in visited    # decline branch NOT taken
        assert "Decision" in visited


class TestDeclinePath:
    """A high-risk applicant flows through to DECLINED."""

    def test_high_risk_applicant_is_declined(self, sm_arn, seeded_policy):
        # Age 70 + tobacco -> risk score clamps to 100 -> DECLINE tier (BR-4/BR-5).
        policy_id = seeded_policy(age=70, tobacco=True)
        arn = start_execution(sm_arn, _event_input(policy_id, age=70, tobacco=True))
        description = wait_for_execution(arn)

        assert description["status"] == "SUCCEEDED"
        assert get_output(description).get("final_status") == "DECLINED"

    def test_decline_path_visits_decline_branch(self, sm_arn, seeded_policy):
        policy_id = seeded_policy(age=70, tobacco=True)
        arn = start_execution(sm_arn, _event_input(policy_id, age=70, tobacco=True))
        wait_for_execution(arn)

        visited = get_visited_states(arn)
        assert "PrepareDecline" in visited        # decline branch chosen
        assert "PrepareApprove" not in visited     # approve branch NOT taken


class TestErrorFallback:
    """A malformed input routes to ManualReview via Catch — not a failed execution."""

    def test_missing_fields_route_to_manual_review(self, sm_arn):
        # Input missing applicant_age etc. The parallel checks will KeyError; the
        # workflow's Catch must route to ManualReview instead of failing the execution.
        bad_input = {"detail": {"policy_id": generate_test_policy_id()}}
        arn = start_execution(sm_arn, bad_input)
        description = wait_for_execution(arn)

        # The execution SUCCEEDS (it handled the error gracefully) ...
        assert description["status"] == "SUCCEEDED", (
            "Workflow should catch the error and reach ManualReview, not FAIL"
        )
        # ... and the path proves it went through the Catch fallback.
        visited = get_visited_states(arn)
        assert "ManualReview" in visited
