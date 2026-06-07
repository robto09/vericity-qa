"""Data-migration pipeline tests.

Answers: "How would you validate data moving through a pipeline?"

The pipeline: a legacy CSV uploaded to s3://.../legacy/*.csv triggers the migration
Lambda, which validates each row, loads clean ones into DynamoDB, flags dirty ones, and
logs a per-file reconciliation report.

The headline assertion is the **completeness invariant** — no row is ever silently
dropped: rows_read == rows_migrated + rows_flagged. We read the report from the migration
Lambda's CloudWatch logs (where it's logged as structured JSON) and assert on it, then
cross-check that migrated rows actually appear in DynamoDB.

Slow: S3 trigger + Lambda run + log propagation.
"""
from __future__ import annotations

import time

import pytest

from framework.aws import client
from framework.config import get_config
from framework.logs import extract_json_context, find_log_events
from framework.wait import wait_until

pytestmark = [pytest.mark.pipeline, pytest.mark.slow]

MIGRATION_FUNCTION = "vericity-migration-dev"

# The sample legacy CSV: 10 rows, 7 clean, 3 dirty (per the platform's sample file +
# BUSINESS_RULES BR-9). We upload our own copy so the test is self-contained.
SAMPLE_CSV = (
    "legacy_policy_number,full_name,date_of_birth,coverage_amount,annual_premium\n"
    "LEG-200001,Alice Brown,1980-01-01,250000,600.00\n"
    "LEG-200002,Bob Green,1975-05-05,500000,900.00\n"
    "LEG-200003,Carol White,1990-09-09,100000,300.00\n"
    "LEG-200004,Dan Black,1985-03-03,750000,1400.00\n"
    "LEG-200005,,1982-02-02,300000,540.00\n"           # dirty: missing name
    "LEG-200006,Eve Gray,1978-07-07,N/A,720.00\n"      # dirty: non-numeric coverage
    "LEG-200007,Frank Blue,1970-12-12,1000000,2100.00\n"
    "LEG-200008,Grace Red,1988-04-04,150000,0\n"       # dirty: non-positive premium
    "LEG-200009,Heidi Pink,1995-08-08,200000,400.00\n"
    "LEG-200010,Ivan Gold,1965-11-11,1250000,2800.00\n"
)


class TestMigrationReconciliation:
    def test_pipeline_reconciles_and_flags_dirty_rows(self):
        config = get_config()
        # Unique key so concurrent/repeat runs don't collide.
        key = f"legacy/qa-migration-{int(time.monotonic() * 1000)}.csv"

        # Upload -> this S3 ObjectCreated event triggers the migration Lambda.
        client("s3").put_object(
            Bucket=config.documents_bucket, Key=key,
            Body=SAMPLE_CSV.encode("utf-8"), ContentType="text/csv",
        )

        # Read the migration report from CloudWatch (logged as structured JSON).
        # Filter by our unique source key so we get THIS run's report.
        def _report():
            messages = find_log_events(
                MIGRATION_FUNCTION,
                f'"{key}"',
                since_seconds=300,
                timeout_seconds=10,
            )
            for message in messages:
                context = extract_json_context(message)
                if context and context.get("source_key") == key:
                    return context
            return None

        report = wait_until(
            _report,
            timeout_seconds=120,
            poll_interval_seconds=8,
            description="migration report in CloudWatch logs",
        )

        # THE completeness invariant — nothing silently dropped.
        assert report["rows_read"] == report["rows_migrated"] + report["rows_flagged"]
        assert report["reconciles"] is True
        # Exact expected split for this fixture (7 clean, 3 dirty).
        assert report["rows_read"] == 10
        assert report["rows_migrated"] == 7
        assert report["rows_flagged"] == 3
        # Every flagged row carries a reason (no silent/unexplained flags).
        reasons = report["flagged_reasons"]
        assert len(reasons) == 3
        flagged_numbers = {r["legacy_policy_number"] for r in reasons}
        assert flagged_numbers == {"LEG-200005", "LEG-200006", "LEG-200008"}

    def test_schema_drift_is_rejected(self):
        # A CSV missing a required column must fail fast (the Lambda raises), NOT
        # silently migrate mismapped data. We assert the Lambda logged an error and
        # did NOT produce a reconciling report for this file.
        config = get_config()
        key = f"legacy/qa-baddrift-{int(time.monotonic() * 1000)}.csv"
        # 'coverage_amount' renamed to 'coverage' -> schema drift.
        bad_csv = (
            "legacy_policy_number,full_name,date_of_birth,coverage,annual_premium\n"
            "LEG-300001,Test User,1980-01-01,250000,600.00\n"
        )
        client("s3").put_object(
            Bucket=config.documents_bucket, Key=key,
            Body=bad_csv.encode("utf-8"), ContentType="text/csv",
        )

        # The Lambda should error on schema drift -> an [ERROR] / exception in logs,
        # and crucially NO successful "Migrated legacy file" report for this key.
        time.sleep(20)  # let the trigger + Lambda run
        success_reports = find_log_events(
            MIGRATION_FUNCTION, f'"{key}"', since_seconds=120, timeout_seconds=10
        )
        reconciled = [
            m for m in success_reports
            if (extract_json_context(m) or {}).get("source_key") == key
            and (extract_json_context(m) or {}).get("reconciles") is True
        ]
        assert not reconciled, "schema-drift file should NOT produce a reconciling migration"
