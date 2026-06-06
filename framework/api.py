"""Thin HTTP client for the deployed REST API.

Wraps `requests` with the base URL resolved from the live stack, so tests call
`api.post_quote(...)` instead of rebuilding URLs and headers every time. This is the
"automate API testing with PyTest" surface — clean, reusable, assertable.
"""
from __future__ import annotations

from typing import Any

import requests

from .config import get_config

# A sensible per-request timeout so a hung endpoint fails the test fast instead of
# blocking the whole suite.
DEFAULT_TIMEOUT_SECONDS = 15


class ApiClient:
    """Typed-ish helpers for each endpoint of the policy API."""

    def __init__(self, base_url: str | None = None) -> None:
        # Default to the live stack's API URL; allow override for targeted testing.
        self.base_url = (base_url or get_config().api_base_url).rstrip("/")

    def post_quote(self, payload: dict[str, Any]) -> requests.Response:
        """POST /quotes — request an instant premium."""
        return requests.post(
            f"{self.base_url}/quotes", json=payload, timeout=DEFAULT_TIMEOUT_SECONDS
        )

    def post_application(self, payload: dict[str, Any]) -> requests.Response:
        """POST /applications — submit an application (starts underwriting)."""
        return requests.post(
            f"{self.base_url}/applications", json=payload, timeout=DEFAULT_TIMEOUT_SECONDS
        )

    def get_policy(self, policy_id: str) -> requests.Response:
        """GET /policies/{id} — read current policy state."""
        return requests.get(
            f"{self.base_url}/policies/{policy_id}", timeout=DEFAULT_TIMEOUT_SECONDS
        )

    # --- Convenience: submit a valid application and return its policy_id. ---
    def create_application(self, **overrides: Any) -> str:
        """Submit a valid application (with optional field overrides) and return its id.

        Used by integration/E2E tests that need a policy in flight without repeating
        the full payload each time.
        """
        payload: dict[str, Any] = {
            "applicant_age": 35,
            "coverage_amount": 500_000,
            "term_years": 20,
            "tobacco_use": False,
            "full_name": "QA Test Applicant",
            "email": "qa@example.com",
        }
        payload.update(overrides)
        response = self.post_application(payload)
        response.raise_for_status()
        return response.json()["policy_id"]
