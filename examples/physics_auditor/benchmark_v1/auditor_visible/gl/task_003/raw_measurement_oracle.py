#!/usr/bin/python3
"""Generic raw-measurement normalizer for a PA-5C1 visible fixture."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: raw_measurement_oracle.py OBSERVATIONS_JSON")
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if set(value) != {"measurements", "schema_version"} or value["schema_version"] != 1:
        raise SystemExit("invalid raw measurement envelope")
    rows = value["measurements"]
    if not isinstance(rows, list) or not rows:
        raise SystemExit("raw measurements are unavailable")
    for row in rows:
        if set(row) != {"name", "uncertainty", "unit", "value"}:
            raise SystemExit("invalid raw measurement row")
        if not isinstance(row["name"], str) or not isinstance(row["unit"], str):
            raise SystemExit("invalid raw measurement metadata")
        if isinstance(row["value"], bool) or isinstance(row["uncertainty"], bool):
            raise SystemExit("raw scalar fields cannot be boolean")
    print(json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True))


if __name__ == "__main__":
    main()
