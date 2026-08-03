import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location("implementation", Path("implementation.py"))
assert spec is not None and spec.loader is not None
implementation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(implementation)
checks = {
    "positive_force": implementation.acceleration(2.0) == 2.0,
    "negative_force": implementation.acceleration(-3.0) == -3.0,
    "zero_force": implementation.acceleration(0.0) == 0.0,
}
print(
    json.dumps(
        {
            "schema_version": 1,
            "oracle_id": "force_oracle",
            "outcome": "passed" if all(checks.values()) else "functional_failure",
            "checks": [
                {"id": key, "passed": value} for key, value in sorted(checks.items())
            ],
        },
        sort_keys=True,
    )
)
