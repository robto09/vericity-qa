"""Functional tests: the Quote API (POST /quotes).

Validates the front door of the system — pricing correctness, response contract, input
validation, and boundary values. This is the "automate API testing with PyTest" surface:
real HTTP calls against the deployed API Gateway endpoint, asserting status codes, the
response schema, and business-rule numbers.

Business rules referenced (from the platform's docs/BUSINESS_RULES.md):
  age 18-75, coverage $25k-$2M, term 10-30; base pricing such that
  age 35 / $500k / 20yr / non-tobacco = $720/yr, $60/mo; tobacco doubles the premium.
"""
from __future__ import annotations

import pytest
from jsonschema import validate

from framework.schemas import QUOTE_RESPONSE_SCHEMA

pytestmark = pytest.mark.functional


class TestQuoteHappyPath:
    """A valid quote request returns a correct, well-formed price."""

    def test_returns_201_and_valid_schema(self, api):
        response = api.post_quote(
            {"applicant_age": 35, "coverage_amount": 500_000, "term_years": 20, "tobacco_use": False}
        )
        assert response.status_code == 201, response.text
        body = response.json()
        # Contract test: one assertion covers the whole response shape.
        validate(instance=body, schema=QUOTE_RESPONSE_SCHEMA)

    def test_known_premium_matches_business_rules(self, api):
        # BR-2.5 worked example A: age 35 / $500k / 20yr / non-tobacco.
        response = api.post_quote(
            {"applicant_age": 35, "coverage_amount": 500_000, "term_years": 20, "tobacco_use": False}
        )
        body = response.json()
        assert body["annual_premium"] == 720
        assert body["monthly_premium"] == 60

    def test_tobacco_doubles_premium(self, api):
        base = api.post_quote(
            {"applicant_age": 35, "coverage_amount": 500_000, "term_years": 20, "tobacco_use": False}
        ).json()
        smoker = api.post_quote(
            {"applicant_age": 35, "coverage_amount": 500_000, "term_years": 20, "tobacco_use": True}
        ).json()
        assert smoker["annual_premium"] == base["annual_premium"] * 2

    def test_older_applicant_pays_more(self, api):
        young = api.post_quote(
            {"applicant_age": 25, "coverage_amount": 250_000, "term_years": 20, "tobacco_use": False}
        ).json()
        old = api.post_quote(
            {"applicant_age": 60, "coverage_amount": 250_000, "term_years": 20, "tobacco_use": False}
        ).json()
        assert old["annual_premium"] > young["annual_premium"]


class TestQuoteValidation:
    """Invalid input is rejected with HTTP 400 and a useful error payload."""

    def test_missing_fields_returns_400_with_all_errors(self, api):
        response = api.post_quote({})
        assert response.status_code == 400
        body = response.json()
        # The API collects ALL field errors, not just the first.
        assert "details" in body
        assert len(body["details"]) == 3  # age, coverage, term all missing

    @pytest.mark.parametrize(
        "field,value",
        [
            ("applicant_age", 17),       # below min 18
            ("applicant_age", 76),       # above max 75
            ("coverage_amount", 24_999),  # below min $25k
            ("coverage_amount", 2_000_001),  # above max $2M
            ("term_years", 9),            # below min 10
            ("term_years", 31),           # above max 30
        ],
    )
    def test_boundary_values_rejected(self, api, field, value):
        # Boundaries are where off-by-one bugs hide — test just-outside each limit.
        payload = {"applicant_age": 35, "coverage_amount": 500_000, "term_years": 20}
        payload[field] = value
        response = api.post_quote(payload)
        assert response.status_code == 400, f"{field}={value} should be rejected"

    @pytest.mark.parametrize(
        "field,value",
        [
            ("applicant_age", 18),        # exactly min — valid
            ("applicant_age", 75),        # exactly max — valid
            ("coverage_amount", 25_000),   # exactly min — valid
            ("term_years", 10),           # exactly min — valid
            ("term_years", 30),           # exactly max — valid
        ],
    )
    def test_boundary_values_accepted(self, api, field, value):
        # The inclusive edges must be ACCEPTED (the other half of boundary testing).
        payload = {"applicant_age": 35, "coverage_amount": 500_000, "term_years": 20}
        payload[field] = value
        response = api.post_quote(payload)
        assert response.status_code == 201, f"{field}={value} should be accepted"

    def test_non_integer_age_rejected(self, api):
        response = api.post_quote(
            {"applicant_age": "thirty", "coverage_amount": 500_000, "term_years": 20}
        )
        assert response.status_code == 400
