"""Data-store accessors for validation: DynamoDB (NoSQL) and RDS (SQL).

These let tests read the system's persisted state directly — the foundation of
"verify data integrity across services." We write via one path (the API/events) and
read back via these to assert the data landed correctly and consistently.

Note on connectivity: these tests run from YOUR machine (not inside the VPC), so:
 - DynamoDB and Secrets Manager are public AWS APIs — reachable directly. ✅
 - RDS is publicly accessible (we set PubliclyAccessible: true) BUT its security group
   only allows the Lambda SG. So a test on your laptop may not be able to connect to
   RDS directly. The RDS helper is provided for completeness / CI-in-VPC use; the
   primary way we validate billing data is via the policy status (ISSUED→ACTIVE) and
   CloudWatch logs, which prove the billing Lambda wrote to RDS. See the journey doc.
"""
from __future__ import annotations

import json
from typing import Any

from .aws import client, resource
from .config import get_config


# --------------------------------------------------------------------------- #
# DynamoDB
# --------------------------------------------------------------------------- #
def get_policy_item(policy_id: str) -> dict[str, Any] | None:
    """Read a policy row from the DynamoDB policies table (or None if absent)."""
    table = resource("dynamodb").Table(get_config().policies_table)
    return table.get_item(Key={"policy_id": policy_id}).get("Item")


def get_quote_item(quote_id: str) -> dict[str, Any] | None:
    """Read a quote row from the DynamoDB quotes table (or None if absent)."""
    table = resource("dynamodb").Table(get_config().quotes_table)
    return table.get_item(Key={"quote_id": quote_id}).get("Item")


def scan_policies_by_source(source: str) -> list[dict[str, Any]]:
    """Return all policies with a given ``source`` (e.g. LEGACY_MIGRATION).

    Used for migration reconciliation — counting migrated rows. A scan is fine here
    because the test dataset is tiny; in production you'd use a GSI.
    """
    table = resource("dynamodb").Table(get_config().policies_table)
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": "#s = :src",
        "ExpressionAttributeNames": {"#s": "source"},
        "ExpressionAttributeValues": {":src": source},
    }
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    return items


# --------------------------------------------------------------------------- #
# RDS (PostgreSQL) — optional / VPC-dependent
# --------------------------------------------------------------------------- #
def _get_db_password() -> str:
    """Fetch the RDS password from Secrets Manager (a public API, reachable)."""
    secret = client("secretsmanager").get_secret_value(SecretId=get_config().db_secret_arn)
    return json.loads(secret["SecretString"])["password"]


def db_connection() -> Any:
    """Open a PostgreSQL connection to the billing DB.

    Only works if your network can reach RDS (its security group must allow your IP,
    or you run in-VPC). If it can't connect, prefer validating billing via policy
    status + CloudWatch logs instead. Imported lazily so the framework loads without
    psycopg2 installed.
    """
    import psycopg2  # noqa: PLC0415

    config = get_config()
    return psycopg2.connect(
        host=config.billing_db_endpoint,
        dbname="vericity",
        user="vericity_app",
        password=_get_db_password(),
        connect_timeout=8,
    )


def query_premium_ledger(policy_id: str) -> list[dict[str, Any]]:
    """Return premium_ledger rows for a policy (requires RDS connectivity)."""
    with db_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT policy_id, due_sequence, amount_due, status "
                "FROM premium_ledger WHERE policy_id = %s ORDER BY due_sequence",
                (policy_id,),
            )
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
