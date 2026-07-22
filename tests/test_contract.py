from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from research_automation_supervisor.contract import MAX_TIMEOUT_SECONDS, load_contract
from research_automation_supervisor.errors import ContractLoadError, ContractValidationError


def valid_contract_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "stage_id": "AUTOMATION-0",
        "title": "Foundation",
        "goal": "Build deterministic validation.",
        "allowed_paths": ["src/**", "tests/**"],
        "protected_paths": ["STAGE_0_CONTRACT.md"],
        "acceptance_tests": [
            {"id": "unit-tests", "command": "pytest -q", "timeout_seconds": 60}
        ],
        "max_repair_rounds": 3,
        "checkpoint_after": True,
    }


def write_contract(path: Path, data: dict[str, object]) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_loads_valid_minimal_yaml(tmp_path: Path) -> None:
    contract = load_contract(write_contract(tmp_path / "stage.yaml", valid_contract_data()))

    assert contract.schema_version == 1
    assert contract.stage_id == "AUTOMATION-0"
    assert contract.acceptance_tests[0].command == "pytest -q"


def test_malformed_yaml_has_useful_location(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("stage_id: [unterminated\n", encoding="utf-8")

    with pytest.raises(ContractLoadError, match=r"malformed YAML at line \d+, column \d+"):
        load_contract(path)


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    data = valid_contract_data()
    data["surprise"] = True

    with pytest.raises(ContractValidationError, match=r"surprise: Extra inputs are not permitted"):
        load_contract(write_contract(tmp_path / "unknown.yaml", data))


def test_duplicate_acceptance_test_ids_are_rejected(tmp_path: Path) -> None:
    data = valid_contract_data()
    data["acceptance_tests"] = [
        {"id": "tests", "command": "pytest -q", "timeout_seconds": 60},
        {"id": "tests", "command": "ruff check .", "timeout_seconds": 60},
    ]

    with pytest.raises(ContractValidationError, match="IDs must be unique; duplicates: tests"):
        load_contract(write_contract(tmp_path / "duplicate.yaml", data))


@pytest.mark.parametrize("repair_rounds", [-1, 11])
def test_invalid_repair_limits_are_rejected(tmp_path: Path, repair_rounds: int) -> None:
    data = valid_contract_data()
    data["max_repair_rounds"] = repair_rounds

    with pytest.raises(ContractValidationError, match="max_repair_rounds"):
        load_contract(write_contract(tmp_path / "repairs.yaml", data))


@pytest.mark.parametrize("timeout", [0, -1, MAX_TIMEOUT_SECONDS + 1])
def test_invalid_timeouts_are_rejected(tmp_path: Path, timeout: int) -> None:
    data = valid_contract_data()
    data["acceptance_tests"] = [
        {"id": "tests", "command": "pytest -q", "timeout_seconds": timeout}
    ]

    with pytest.raises(ContractValidationError, match="timeout_seconds"):
        load_contract(write_contract(tmp_path / "timeout.yaml", data))


def test_allowed_protected_conflict_uses_normalized_patterns(tmp_path: Path) -> None:
    data = valid_contract_data()
    data["allowed_paths"] = [" ./src//** "]
    data["protected_paths"] = ["src/**"]

    with pytest.raises(ContractValidationError, match=r"overlap after normalization: src/\*\*"):
        load_contract(write_contract(tmp_path / "paths.yaml", data))


def test_patterns_are_normalized_in_model(tmp_path: Path) -> None:
    data = valid_contract_data()
    data["allowed_paths"] = [r"src\package\..\other\**"]

    contract = load_contract(write_contract(tmp_path / "normalized.yaml", data))

    assert contract.allowed_paths == ["src/other/**"]


@pytest.mark.parametrize(
    ("field", "value"),
    [("stage_id", "bad id"), ("title", "  "), ("goal", "")],
)
def test_invalid_required_strings_are_rejected(
    tmp_path: Path, field: str, value: str
) -> None:
    data = valid_contract_data()
    data[field] = value

    with pytest.raises(ContractValidationError, match=field):
        load_contract(write_contract(tmp_path / "string.yaml", data))


def test_missing_file_is_a_load_error(tmp_path: Path) -> None:
    with pytest.raises(ContractLoadError, match="could not read contract"):
        load_contract(tmp_path / "missing.yaml")
