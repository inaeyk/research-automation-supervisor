#!/usr/bin/python3
"""Fixed public oracle for the PA-5B synthetic cases.

The Physics Auditor never receives this executable. PA-2 seals and runs it read-only,
then PA-3 exposes only the bounded structured check result and proof hashes.
"""

from __future__ import annotations

import json
import sys

PASSING_CASES = {"clean_reference", "correct_alternative", "insufficient_evidence"}


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: benchmark_oracle.py SEED_KIND ORACLE_ID")
    seed_kind, oracle_id = sys.argv[1:]
    primary_passed = seed_kind in PASSING_CASES
    if seed_kind == "conflicting_evidence":
        primary_passed = oracle_id == "case_oracle_a"
    checks = {"control": True, "primary": primary_passed}
    print(
        json.dumps(
            {
                "schema_version": 1,
                "oracle_id": oracle_id,
                "outcome": "passed" if all(checks.values()) else "functional_failure",
                "checks": [
                    {"id": key, "passed": value}
                    for key, value in sorted(checks.items())
                ],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

