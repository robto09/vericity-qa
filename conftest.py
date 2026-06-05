"""Pytest fixtures and configuration shared across the whole QA suite.

Fixtures here give every test easy, consistent access to the live stack (config, API
client) and register custom markers so you can run subsets like
`pytest -m "events"` or skip slow ones with `pytest -m "not slow"`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the `framework` package importable from anywhere in the suite.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from framework.api import ApiClient  # noqa: E402
from framework.config import get_config  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers so `pytest --markers` documents the suite layout."""
    markers = [
        "functional: single-service functional/behavioral tests",
        "integration: service-to-service integration tests",
        "events: EventBridge publish/schema/delivery/ordering tests",
        "workflow: Step Functions workflow tests",
        "async_: SQS asynchronous + retry/DLQ tests ('async' is a Python keyword, so the marker is async_)",
        "data: data integrity / consistency / SQL+NoSQL validation",
        "pipeline: data-migration pipeline tests",
        "monitoring: CloudWatch log / observability verification",
        "slow: tests that wait on async propagation (tens of seconds)",
        "rds: requires direct network connectivity to RDS (may skip off-VPC)",
    ]
    for marker in markers:
        config.addinivalue_line("markers", marker)


@pytest.fixture(scope="session")
def config():
    """The live stack configuration (resolved once per test session)."""
    return get_config()


@pytest.fixture(scope="session")
def api():
    """A ready-to-use API client pointed at the live stack."""
    return ApiClient()


@pytest.fixture
def seeded_policy():
    """Factory that creates a real DynamoDB policy row and cleans it up after the test.

    The underwriting workflow's merge/decision steps UPDATE an existing policy row, so
    tests that start the state machine directly (bypassing the API) must first seed a
    row. This factory returns a function `seeded_policy(age=.., tobacco=..)` -> policy_id
    and tracks every id it creates to delete on teardown (no orphaned test data).
    """
    from decimal import Decimal

    from framework.aws import resource
    from framework.config import get_config
    from framework.models import generate_test_policy_id

    table = resource("dynamodb").Table(get_config().policies_table)
    created_ids: list[str] = []

    def _create(age: int = 35, tobacco: bool = False, coverage: int = 250_000) -> str:
        policy_id = generate_test_policy_id()
        table.put_item(
            Item={
                "policy_id": policy_id,
                "status": "APPLIED",
                "applicant_age": age,
                "coverage_amount": coverage,
                "term_years": 20,
                "tobacco_use": tobacco,
                "full_name": "Workflow Test",
                "email": "workflow@example.com",
                "quoted_annual_premium": Decimal("0"),
                "quoted_monthly_premium": Decimal("0"),
            }
        )
        created_ids.append(policy_id)
        return policy_id

    yield _create

    # Teardown: remove everything this test created.
    for policy_id in created_ids:
        try:
            table.delete_item(Key={"policy_id": policy_id})
        except Exception:  # noqa: BLE001
            pass
