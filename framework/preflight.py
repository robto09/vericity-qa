"""Preflight check — verify the live stack is reachable before running tests.

Run as `python -m framework.preflight`. Exits 0 if the stack is deployed, RDS is
available, and the API answers; non-zero with a clear message otherwise. This turns a
pile of confusing mid-suite failures (when the stack is down or RDS is stopped) into one
upfront, actionable error — both locally and in CI.
"""
from __future__ import annotations

import sys

import requests

from .aws import client
from .config import STACK_NAME, get_config


def main() -> int:
    print(f"Preflight: checking stack '{STACK_NAME}' ...")

    # 1. Stack outputs resolvable (stack deployed)?
    try:
        config = get_config()
    except Exception as error:  # noqa: BLE001
        print(f"FAIL: cannot read stack outputs — is it deployed?\n  {error}")
        return 1
    print(f"  stack outputs OK (API: {config.api_base_url})")

    # 2. RDS available? (billing/ACTIVE tests need it running, not stopped.)
    try:
        rds = client("rds")
        # The DB identifier is derivable from the endpoint's first label.
        db_id = config.billing_db_endpoint.split(".")[0]
        status = rds.describe_db_instances(DBInstanceIdentifier=db_id)[
            "DBInstances"
        ][0]["DBInstanceStatus"]
        if status != "available":
            print(
                f"FAIL: RDS '{db_id}' is '{status}', not 'available'. "
                f"Start it: ../scripts/db-power.sh start"
            )
            return 1
        print(f"  RDS available ({db_id})")
    except Exception as error:  # noqa: BLE001
        print(f"FAIL: cannot check RDS status: {error}")
        return 1

    # 3. API actually answering? (A cheap, valid quote call.)
    try:
        response = requests.post(
            f"{config.api_base_url}/quotes",
            json={"applicant_age": 35, "coverage_amount": 500_000, "term_years": 20},
            timeout=15,
        )
        if response.status_code != 201:
            print(f"FAIL: API /quotes returned {response.status_code}: {response.text}")
            return 1
        print("  API responding OK")
    except Exception as error:  # noqa: BLE001
        print(f"FAIL: API not reachable: {error}")
        return 1

    print("Preflight OK — the live stack is ready for testing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
