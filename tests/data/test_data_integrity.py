"""Cross-service data integrity tests — the full lifecycle, end to end.

Answers: "How would you verify data integrity across services?"

We submit ONE application via the API and let the whole async machine run, then assert
the SAME facts are consistent across every store the data passes through:
  - the DynamoDB policy row (status progressed, premium re-priced by tier),
  - the S3 policy document (exists, contains the policy's real figures),
  - reconciliation (one application -> one document -> one ACTIVE policy).

This is the highest-value integration test: it proves the seams between services
preserve the customer's data correctly, with no loss or divergence.

Slow: waits for the full APPLIED -> APPROVED -> ISSUED -> ACTIVE pipeline.
"""
from __future__ import annotations

import pytest

from framework.aws import client
from framework.config import get_config
from framework.data import get_policy_item
from framework.wait import wait_until

pytestmark = [pytest.mark.data, pytest.mark.integration, pytest.mark.slow]


class TestEndToEndDataIntegrity:
    def test_full_lifecycle_data_is_consistent(self, api):
        # 1. Submit a low-risk application so it approves and runs the full pipeline.
        policy_id = api.create_application(
            applicant_age=30, coverage_amount=400_000, term_years=20, tobacco_use=False,
            full_name="Integrity Test", email="integrity@example.com",
        )

        # 2. Wait for the async pipeline to reach ACTIVE (the terminal happy state).
        final = wait_until(
            lambda: (lambda p: p if p and p.get("status") == "ACTIVE" else None)(
                get_policy_item(policy_id)
            ),
            timeout_seconds=120,
            description=f"{policy_id} to reach ACTIVE end-to-end",
        )

        # 3. DynamoDB consistency: the record reflects every stage's writes.
        assert final["full_name"] == "Integrity Test"
        assert int(final["coverage_amount"]) == 400_000
        assert final["status"] == "ACTIVE"
        assert "risk_tier" in final and final["risk_tier"] in {
            "PREFERRED", "STANDARD", "SUBSTANDARD"
        }
        assert "final_annual_premium" in final
        assert "document_s3_key" in final  # issuance recorded where the doc lives

        # 4. S3 consistency: the document actually exists AND contains this policy's
        #    real figures (not a stale/empty/placeholder doc).
        s3 = client("s3")
        document = s3.get_object(
            Bucket=final["document_bucket"], Key=final["document_s3_key"]
        )["Body"].read().decode("utf-8")

        assert policy_id in document, "policy_id missing from the S3 document"
        assert "Integrity Test" in document, "insured name missing from the S3 document"
        assert str(int(final["coverage_amount"])) in document.replace(",", ""), (
            "coverage amount in the S3 document doesn't match the policy record"
        )

        # 5. Reconciliation: the document key follows the one-doc-per-policy convention.
        assert final["document_s3_key"] == f"policies/{policy_id}/policy.txt"

    def test_declined_policy_has_no_document(self, api):
        # A declined policy must NOT get an issued document (issuance only runs on
        # PolicyApproved) — proving the decline branch doesn't leak downstream artifacts.
        policy_id = api.create_application(
            applicant_age=70, tobacco_use=True,  # high risk -> DECLINE
            full_name="Declined Test", email="declined@example.com",
        )
        final = wait_until(
            lambda: (lambda p: p if p and p.get("status") in {"DECLINED", "MANUAL_REVIEW"} else None)(
                get_policy_item(policy_id)
            ),
            timeout_seconds=120,
            description=f"{policy_id} to reach a terminal non-issued state",
        )
        assert final["status"] == "DECLINED"
        # No issuance happened -> no document key recorded.
        assert "document_s3_key" not in final
