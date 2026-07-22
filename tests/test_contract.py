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

    assert contract.allowed_paths == ("src/other/**",)


def test_duplicate_top_level_yaml_key_is_rejected(tmp_path: Path) -> None:
    source = yaml.safe_dump(valid_contract_data(), sort_keys=False)
    source = source.replace(
        "schema_version: 1\n", "schema_version: 1\nschema_version: 1\n", 1
    )
    path = tmp_path / "duplicate-top-level.yaml"
    path.write_text(source, encoding="utf-8")

    with pytest.raises(ContractLoadError, match="duplicate mapping key"):
        load_contract(path)


def test_duplicate_nested_yaml_key_is_rejected(tmp_path: Path) -> None:
    source = yaml.safe_dump(valid_contract_data(), sort_keys=False)
    source = source.replace(
        "  command: pytest -q\n",
        "  command: pytest -q\n  command: ruff check .\n",
        1,
    )
    path = tmp_path / "duplicate-nested.yaml"
    path.write_text(source, encoding="utf-8")

    with pytest.raises(ContractLoadError, match="duplicate mapping key"):
        load_contract(path)


def test_invalid_utf8_is_a_sanitized_load_error(tmp_path: Path) -> None:
    path = tmp_path / "invalid-utf8.yaml"
    path.write_bytes(b"schema_version: \xff")

    with pytest.raises(
        ContractLoadError, match=r"contract is not valid UTF-8 at byte offset \d+"
    ):
        load_contract(path)


def test_unsafe_python_yaml_tag_is_rejected_and_sanitized(tmp_path: Path) -> None:
    sentinel = "AUDIT_SECRET_SENTINEL"
    path = tmp_path / "unsafe-tag.yaml"
    path.write_text(
        "goal: !!python/object/apply:os.system " f"['{sentinel}']\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractLoadError) as error:
        load_contract(path)

    assert "could not determine a constructor" in str(error.value)
    assert sentinel not in str(error.value)


def test_nested_unknown_fields_are_rejected(tmp_path: Path) -> None:
    data = valid_contract_data()
    data["acceptance_tests"] = [
        {
            "id": "tests",
            "command": "pytest -q",
            "timeout_seconds": 60,
            "working_directory": "/tmp",
        }
    ]

    with pytest.raises(
        ContractValidationError,
        match=r"acceptance_tests\.0\.working_directory: Extra inputs are not permitted",
    ):
        load_contract(write_contract(tmp_path / "nested-extra.yaml", data))


def test_blank_acceptance_command_is_rejected(tmp_path: Path) -> None:
    data = valid_contract_data()
    data["acceptance_tests"] = [
        {"id": "tests", "command": " \t ", "timeout_seconds": 60}
    ]

    with pytest.raises(ContractValidationError, match=r"acceptance_tests\.0\.command"):
        load_contract(write_contract(tmp_path / "blank-command.yaml", data))


@pytest.mark.parametrize("field", ["allowed_paths", "protected_paths"])
def test_blank_path_patterns_are_rejected(tmp_path: Path, field: str) -> None:
    data = valid_contract_data()
    data[field] = [" \t "]

    with pytest.raises(ContractValidationError, match=f"{field}.0"):
        load_contract(write_contract(tmp_path / f"blank-{field}.yaml", data))


@pytest.mark.parametrize("identifier", ["bad id", ".hidden", "tests/fast"])
def test_invalid_acceptance_test_identifiers_are_rejected(
    tmp_path: Path, identifier: str
) -> None:
    data = valid_contract_data()
    data["acceptance_tests"] = [
        {"id": identifier, "command": "pytest -q", "timeout_seconds": 60}
    ]

    with pytest.raises(ContractValidationError, match=r"acceptance_tests\.0\.id"):
        load_contract(write_contract(tmp_path / "invalid-test-id.yaml", data))


@pytest.mark.parametrize(
    ("repair_rounds", "timeout"),
    [(0, 1), (10, MAX_TIMEOUT_SECONDS)],
)
def test_numeric_boundary_values_are_accepted(
    tmp_path: Path, repair_rounds: int, timeout: int
) -> None:
    data = valid_contract_data()
    data["max_repair_rounds"] = repair_rounds
    data["acceptance_tests"] = [
        {"id": "boundary", "command": "pytest -q", "timeout_seconds": timeout}
    ]

    contract = load_contract(write_contract(tmp_path / "boundaries.yaml", data))

    assert contract.max_repair_rounds == repair_rounds
    assert contract.acceptance_tests[0].timeout_seconds == timeout


def test_validated_contract_collections_are_immutable(tmp_path: Path) -> None:
    contract = load_contract(write_contract(tmp_path / "immutable.yaml", valid_contract_data()))

    assert isinstance(contract.allowed_paths, tuple)
    assert isinstance(contract.protected_paths, tuple)
    assert isinstance(contract.acceptance_tests, tuple)
    with pytest.raises(AttributeError):
        contract.allowed_paths.append("protected/**")
    with pytest.raises(AttributeError):
        contract.protected_paths.append("src/**")
    with pytest.raises(AttributeError):
        contract.acceptance_tests.append(contract.acceptance_tests[0])


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
