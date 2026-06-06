"""Small test-data helpers (IDs, sample payloads)."""
from __future__ import annotations

import time


def generate_test_policy_id() -> str:
    """Generate a unique, recognizable test policy id.

    Prefixed with 'pol_qa' so test-created records are easy to spot (and clean up)
    versus real ones. Uses a monotonic clock for uniqueness within a run.
    """
    return f"pol_qa{int(time.monotonic() * 1_000_000)}"
