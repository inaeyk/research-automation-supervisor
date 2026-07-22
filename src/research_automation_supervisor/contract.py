"""Typed models and loading for deterministic stage contracts."""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml  # type: ignore[import-untyped]
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from research_automation_supervisor.errors import ContractLoadError, ContractValidationError

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
MAX_TIMEOUT_SECONDS = 86_400


def normalize_path_pattern(value: str) -> str:
    """Return a stable POSIX-style representation of a path pattern."""
    stripped = value.strip()
    if not stripped:
        raise ValueError("path patterns must not be empty")
    return posixpath.normpath(stripped.replace("\\", "/"))


PathPattern = Annotated[str, AfterValidator(normalize_path_pattern)]
Identifier = Annotated[str, Field(min_length=1, pattern=IDENTIFIER_PATTERN)]
RequiredString = Annotated[str, Field(min_length=1)]


class AcceptanceTest(BaseModel):
    """One deterministic command used to accept a completed stage."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, strict=True)

    id: Identifier
    command: RequiredString
    timeout_seconds: Annotated[int, Field(gt=0, le=MAX_TIMEOUT_SECONDS)]


class StageContract(BaseModel):
    """The frozen schema-version-1 stage contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True, strict=True)

    schema_version: Literal[1]
    stage_id: Identifier
    title: RequiredString
    goal: RequiredString
    allowed_paths: list[PathPattern]
    protected_paths: list[PathPattern]
    acceptance_tests: list[AcceptanceTest]
    max_repair_rounds: Annotated[int, Field(ge=0, le=10)]
    checkpoint_after: bool

    @model_validator(mode="after")
    def validate_cross_field_constraints(self) -> StageContract:
        test_ids = [test.id for test in self.acceptance_tests]
        duplicate_ids = sorted({test_id for test_id in test_ids if test_ids.count(test_id) > 1})
        if duplicate_ids:
            joined = ", ".join(duplicate_ids)
            raise ValueError(f"acceptance-test IDs must be unique; duplicates: {joined}")

        conflicts = sorted(set(self.allowed_paths) & set(self.protected_paths))
        if conflicts:
            joined = ", ".join(conflicts)
            raise ValueError(
                "allowed_paths and protected_paths overlap after normalization: " f"{joined}"
            )
        return self


def load_contract(path: Path) -> StageContract:
    """Read, parse, and validate a YAML stage contract without modifying it."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractLoadError(f"could not read contract '{path}': {exc.strerror or exc}") from exc

    try:
        data: Any = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise ContractLoadError(f"malformed YAML{location}: {problem}") from exc

    if not isinstance(data, dict):
        raise ContractValidationError("contract root must be a YAML mapping")

    try:
        return StageContract.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(_format_validation_error(error) for error in exc.errors())
        raise ContractValidationError(f"contract validation failed: {details}") from exc


def _format_validation_error(error: Mapping[str, Any]) -> str:
    location = ".".join(str(part) for part in error["loc"]) or "contract"
    return f"{location}: {error['msg']}"
