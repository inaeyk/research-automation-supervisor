#!/usr/bin/python3
"""Fixed public PA-2 oracle for bounded GL pilot snapshots."""

from __future__ import annotations

import json
import sys

PASSING_TASKS = {
    "gl_pilot_001",
    "gl_pilot_002",
    "gl_pilot_003",
    "gl_pilot_004",
    "gl_pilot_005",
    "gl_pilot_010",
}


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: gl_pilot_oracle.py TASK_ID ORACLE_ID")
    task_id, oracle_id = sys.argv[1:]
    passed = task_id in PASSING_TASKS
    checks = {"control": True, "primary": passed}
    print(json.dumps({
        "schema_version": 1,
        "oracle_id": oracle_id,
        "outcome": "passed" if all(checks.values()) else "functional_failure",
        "checks": [
            {"id": key, "passed": value}
            for key, value in sorted(checks.items())
        ],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
