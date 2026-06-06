"""Integration tests: API → DynamoDB persistence.

Functional tests proved the API *responds* correctly. These prove the data is actually
*persisted* in DynamoDB with the right fields — the first link in "verify data integrity
across services." We write via the API and read back directly from the table.
"""
from __future__ import annotations

import pytest

from framework.data import get_policy_item, get_quote_item

pytestmark = pytest.mark.integration


class TestQuotePersistence:
    def test_quote_is_persisted_with_matching_fields(self, api):
        response = api.post_quote(
            {"applicant_age": 40, "coverage_amount": 300_000, "term_years": 20, "tobacco_use": False}
        )
        quote_id = response.json()["quote_id"]

        # Read the row straight from DynamoDB and confirm it matches what we sent +
        # what the API returned (the persisted record is the source of truth).
        item = get_quote_item(quote_id)
        assert item is not None, "quote not found in DynamoDB"
        assert int(item["applicant_age"]) == 40
        assert int(item["coverage_amount"]) == 300_000
        assert item["status"] == "QUOTED"
        # Premium stored as Decimal — compare numerically to the API's value.
        assert float(item["annual_premium"]) == float(response.json()["annual_premium"])


class TestApplicationPersistence:
    def test_application_is_persisted_as_APPLIED(self, api):
        policy_id = api.create_application(full_name="Persist Test", email="persist@example.com")
        item = get_policy_item(policy_id)
        assert item is not None, "policy not found in DynamoDB"
        # Identity fields stored correctly.
        assert item["full_name"] == "Persist Test"
        assert item["email"] == "persist@example.com"
        # Status starts at APPLIED (pre-underwriting). Note: by the time we read, the
        # async workflow may have advanced it — so we accept APPLIED or any later
        # legal status, proving the record exists and entered the lifecycle.
        assert item["status"] in {
            "APPLIED", "UNDERWRITING", "APPROVED", "DECLINED", "ISSUED", "ACTIVE", "MANUAL_REVIEW"
        }

    def test_unknown_policy_returns_404(self, api):
        response = api.get_policy("pol_does_not_exist_000")
        assert response.status_code == 404
