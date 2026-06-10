"""Test configuration — discovers the live stack's resources from CloudFormation.

The whole point of this framework is to test the REAL deployed system, not mocks. So
instead of hardcoding the API URL, table names, queue URLs, etc. (which change on every
deploy), we read them from the deployed CloudFormation stack's Outputs at runtime.

This means the same test suite works against any deployment (dev/prod) without edits —
you just point it at a stack name. That's exactly how a real QA framework stays portable
across environments.
"""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from typing import Any

import boto3

# Which stack + region to test. Overridable via env vars so CI can target a
# different stack than your laptop without code changes.
STACK_NAME: str = os.environ.get("VERICITY_STACK", "vericity-dev")
AWS_REGION: str = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))


@dataclass(frozen=True)
class StackConfig:
    """The live resources under test, resolved from CloudFormation stack outputs.

    Every field maps to an Output the platform's template.yaml exposes. Frozen so a
    test can't accidentally mutate shared config.
    """

    api_base_url: str
    policies_table: str
    quotes_table: str
    event_bus_name: str
    billing_queue_url: str
    billing_dlq_url: str
    documents_bucket: str
    underwriting_state_machine_arn: str
    batch_migration_state_machine_arn: str
    billing_db_endpoint: str
    db_secret_arn: str
    stage: str
    region: str = AWS_REGION

    def lambda_function_name(self, short_name: str) -> str:
        """Return a platform Lambda's deployed name (template: vericity-<name>-<stage>).

        Used by tests that read CloudWatch logs, so the suite stays portable across
        stages instead of hardcoding names like 'vericity-billing-dev'.
        """
        return f"vericity-{short_name}-{self.stage}"


def _describe_stack(stack_name: str, region: str) -> dict[str, Any]:
    """Return the deployed stack's describe-stacks entry (Outputs + Parameters).

    Raises a clear error if the stack isn't deployed — the most common reason a QA
    run fails to even start ("did you deploy + start RDS first?").
    """
    cloudformation = boto3.client("cloudformation", region_name=region)
    try:
        response = cloudformation.describe_stacks(StackName=stack_name)
    except cloudformation.exceptions.ClientError as error:
        raise RuntimeError(
            f"Cannot read stack '{stack_name}' in {region}: {error}. "
            f"Is it deployed? Run ../scripts/deploy.sh in the platform repo first."
        ) from error
    return response["Stacks"][0]


@functools.lru_cache(maxsize=1)
def get_config() -> StackConfig:
    """Load (and cache) the live stack configuration for the whole test session.

    Cached so we hit CloudFormation once per run, not once per test.
    """
    stack = _describe_stack(STACK_NAME, AWS_REGION)
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack.get("Outputs", [])}
    parameters = {
        item["ParameterKey"]: item["ParameterValue"] for item in stack.get("Parameters", [])
    }

    # Helper to fetch a required output with a helpful error if it's missing
    # (e.g. the template changed and an Output was renamed).
    def required(key: str) -> str:
        value = outputs.get(key)
        if not value:
            raise RuntimeError(
                f"Stack output '{key}' missing from '{STACK_NAME}'. "
                f"Available outputs: {sorted(outputs)}"
            )
        return value

    return StackConfig(
        api_base_url=required("ApiBaseUrl").rstrip("/"),
        policies_table=required("PoliciesTableName"),
        quotes_table=required("QuotesTableName"),
        event_bus_name=required("EventBusName"),
        billing_queue_url=required("BillingQueueUrl"),
        billing_dlq_url=required("BillingDlqUrl"),
        documents_bucket=required("DocumentsBucketName"),
        underwriting_state_machine_arn=required("UnderwritingStateMachineArn"),
        batch_migration_state_machine_arn=required("BatchMigrationStateMachineArn"),
        billing_db_endpoint=required("BillingDbEndpoint"),
        db_secret_arn=required("DbSecretArn"),
        # The deployment stage drives resource naming (vericity-<name>-<stage>); read
        # it from the stack's own parameters so nothing in the suite hardcodes "dev".
        stage=parameters.get("Stage", "dev"),
    )
