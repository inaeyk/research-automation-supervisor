"""Strict immutable models and loading for deterministic Stage 2 workflows."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import stat
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, TypeVar

import yaml  # type: ignore[import-untyped]
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from research_automation_supervisor.codex_models import ModelName, ReasoningEffort
from research_automation_supervisor.contract import _format_validation_error, _UniqueKeySafeLoader
from research_automation_supervisor.errors import WorkflowDependencyError, WorkflowInputError
from research_automation_supervisor.redaction import is_sensitive_name, would_redact_text

MAX_HUMAN_FILE_BYTES = 2 * 1024 * 1024
MAX_TEST_OUTPUT_BYTES = 100 * 1024 * 1024
MAX_STRUCTURED_STRING_BYTES = 16 * 1024
MIN_MODEL_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 14_400

Identifier = Annotated[
    str,
    Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$"),
]
RequiredString = Annotated[str, Field(min_length=1)]
BoundedString = Annotated[
    str,
    AfterValidator(lambda value: _sanitize_structured_string(value)),
    Field(min_length=1, max_length=MAX_STRUCTURED_STRING_BYTES),
]
WorkflowStatus = Literal[
    "initialized",
    "worker_running",
    "scope_checking",
    "tests_running",
    "auditor_running",
    "repair_pending",
    "human_paused",
    "repair_limit_paused",
    "checkpoint_paused",
    "completed",
    "failed",
    "aborted",
]

TERMINAL_STATUSES = frozenset({"checkpoint_paused", "completed", "failed", "aborted"})
PAUSED_STATUSES = frozenset({"human_paused", "repair_limit_paused"})
ACTIVE_STATUSES = frozenset(
    {
        "worker_running",
        "scope_checking",
        "tests_running",
        "auditor_running",
        "repair_pending",
    }
)


def _freeze_sequence(value: Any) -> Any:
    return tuple(value) if isinstance(value, list) else value


def _sanitize_structured_string(value: str) -> str:
    normalized = "".join(
        character
        for character in value
        if character in {"\n", "\t"} or ord(character) >= 32
    ).strip()
    if not normalized:
        raise ValueError("structured strings must not be empty after sanitization")
    return normalized


def normalize_relative_path(value: str) -> str:
    """Normalize one nonempty POSIX-style relative path without traversal."""
    stripped = value.strip().replace("\\", "/")
    if not stripped:
        raise ValueError("relative paths must not be empty")
    if any(ord(character) < 32 for character in stripped):
        raise ValueError("relative paths must not contain control characters")
    pure = PurePosixPath(stripped)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise ValueError("relative paths must not be absolute or contain '..'")
    normalized = posixpath.normpath(stripped)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise ValueError("relative paths must identify a path below the workspace")
    return normalized


def normalize_path_pattern(value: str) -> str:
    """Normalize one safe relative POSIX path pattern."""
    stripped = value.strip().replace("\\", "/")
    if not stripped:
        raise ValueError("path patterns must not be empty")
    if any(ord(character) < 32 for character in stripped) or re.match(
        r"^[A-Za-z]:/", stripped
    ):
        raise ValueError("path patterns must be POSIX-style relative patterns")
    pure = PurePosixPath(stripped)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise ValueError("path patterns must not be absolute or contain '..'")
    normalized = posixpath.normpath(stripped)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise ValueError("path patterns must identify paths below the workspace")
    return normalized


PathPattern = Annotated[
    str, BeforeValidator(normalize_path_pattern), Field(min_length=1)
]
PathPatterns = Annotated[tuple[PathPattern, ...], BeforeValidator(_freeze_sequence)]
Argv = Annotated[tuple[RequiredString, ...], BeforeValidator(_freeze_sequence)]


class WorkflowTest(BaseModel):
    """One fixed, shell-free acceptance test."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    id: Identifier
    argv: Argv
    cwd: RequiredString
    timeout_seconds: Annotated[int, Field(ge=1, le=MAX_TIMEOUT_SECONDS)]
    max_stdout_bytes: Annotated[int, Field(ge=1, le=MAX_TEST_OUTPUT_BYTES)]
    max_stderr_bytes: Annotated[int, Field(ge=1, le=MAX_TEST_OUTPUT_BYTES)]

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("argv must contain at least one element")
        if any(not item.strip() for item in value):
            raise ValueError("argv elements must not be empty")
        if any(any(ord(character) < 32 for character in item) for item in value):
            raise ValueError("argv elements must not contain control characters")
        return value


WorkflowTests = Annotated[tuple[WorkflowTest, ...], BeforeValidator(_freeze_sequence)]


class SubstageSpecification(BaseModel):
    """The exact frozen schema-version-1 substage specification."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: Literal[1]
    substage_id: Identifier
    title: RequiredString
    workspace: RequiredString
    contract_path: RequiredString
    worker_initial_prompt_path: RequiredString
    worker_repair_prompt_path: RequiredString
    auditor_prompt_path: RequiredString
    worker_model: ModelName
    worker_reasoning_effort: ReasoningEffort
    worker_timeout_seconds: Annotated[
        int, Field(ge=MIN_MODEL_TIMEOUT_SECONDS, le=MAX_TIMEOUT_SECONDS)
    ]
    auditor_model: ModelName
    auditor_reasoning_effort: ReasoningEffort
    auditor_timeout_seconds: Annotated[
        int, Field(ge=MIN_MODEL_TIMEOUT_SECONDS, le=MAX_TIMEOUT_SECONDS)
    ]
    acceptance_tests: WorkflowTests
    allowed_paths: PathPatterns
    protected_paths: PathPatterns
    max_repair_rounds: Annotated[int, Field(ge=0, le=10)]
    checkpoint_after: bool

    @model_validator(mode="after")
    def validate_collections(self) -> SubstageSpecification:
        test_ids = [test.id for test in self.acceptance_tests]
        duplicates = sorted({item for item in test_ids if test_ids.count(item) > 1})
        if duplicates:
            raise ValueError(f"acceptance-test IDs must be unique: {', '.join(duplicates)}")
        if len(set(self.allowed_paths)) != len(self.allowed_paths):
            raise ValueError("allowed_paths contains a duplicate normalized pattern")
        if len(set(self.protected_paths)) != len(self.protected_paths):
            raise ValueError("protected_paths contains a duplicate normalized pattern")
        overlap = sorted(set(self.allowed_paths) & set(self.protected_paths))
        if overlap:
            raise ValueError(
                "allowed_paths and protected_paths overlap after normalization: "
                + ", ".join(overlap)
            )
        return self


class WorkerModelResult(BaseModel):
    """Validated structured result from a worker turn."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: Literal[1]
    status: Literal["completed", "blocked", "needs_human"]
    summary: BoundedString
    changed_files: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence), Field(max_length=200)
    ]
    assumptions: Annotated[
        tuple[BoundedString, ...], BeforeValidator(_freeze_sequence), Field(max_length=200)
    ]
    questions: Annotated[
        tuple[BoundedString, ...], BeforeValidator(_freeze_sequence), Field(max_length=200)
    ]

    @field_validator("changed_files")
    @classmethod
    def normalize_changed_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(normalize_relative_path(item) for item in value)
        if len(set(normalized)) != len(normalized):
            raise ValueError("changed_files contains duplicate normalized paths")
        return normalized


class AuditFinding(BaseModel):
    """One validated, bounded auditor finding."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    id: Identifier
    severity: Literal["critical", "high", "medium", "low"]
    category: Annotated[
        str,
        AfterValidator(lambda value: _sanitize_structured_string(value)),
        Field(min_length=1, max_length=256),
    ]
    file: str | None
    line: Annotated[int, Field(gt=0)] | None
    evidence: BoundedString
    required_fix: BoundedString

    @field_validator("file")
    @classmethod
    def normalize_file(cls, value: str | None) -> str | None:
        return None if value is None else normalize_relative_path(value)


class AuditorModelResult(BaseModel):
    """Validated structured result from one fresh auditor turn."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    schema_version: Literal[1]
    verdict: Literal["pass", "fail_repairable", "escalate"]
    summary: BoundedString
    scope_compliant: bool
    contract_satisfied: bool
    findings: Annotated[
        tuple[AuditFinding, ...], BeforeValidator(_freeze_sequence), Field(max_length=200)
    ]
    human_questions: Annotated[
        tuple[BoundedString, ...], BeforeValidator(_freeze_sequence), Field(max_length=200)
    ]

    @model_validator(mode="after")
    def validate_verdict(self) -> AuditorModelResult:
        identifiers = [finding.id for finding in self.findings]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("auditor finding IDs must be unique")
        if self.verdict == "pass" and (
            self.findings
            or self.human_questions
            or not self.scope_compliant
            or not self.contract_satisfied
        ):
            raise ValueError(
                "pass requires no findings or human questions and both compliance flags true"
            )
        if self.verdict == "fail_repairable" and not self.findings:
            raise ValueError("fail_repairable requires at least one finding")
        return self


class WorkflowResult(BaseModel):
    """Stable public workflow result mirrored by state and CLI output."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    substage_id: Identifier
    run_token: Identifier
    status: WorkflowStatus
    repair_round: Annotated[int, Field(ge=0)]
    max_repair_rounds: Annotated[int, Field(ge=0, le=10)]
    checkpoint_after: bool
    workspace: RequiredString
    baseline_commit: Annotated[str, Field(pattern=r"^[0-9a-fA-F]{40,64}$")]
    worker_thread_id: Annotated[str, Field(max_length=256)] | None
    latest_worker_action_id: str | None
    latest_audit_action_id: str | None
    tests_passed: bool
    scope_compliant: bool
    contract_satisfied: bool
    artifact_directory: RequiredString
    pause_reason: Annotated[str, Field(max_length=MAX_STRUCTURED_STRING_BYTES)] | None
    summary: BoundedString
    started_at: RequiredString
    updated_at: RequiredString

    def to_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class PendingAction(BaseModel):
    """Durable intent for one possibly in-flight external action."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action_id: Identifier
    kind: Literal["worker", "auditor", "test"]
    repair_round: Annotated[int, Field(ge=0)]
    run_id: Identifier
    artifact_path: RequiredString
    workspace: RequiredString
    role: Literal["worker", "auditor", "fixed_test"]
    codex_executable: str | None
    model: ModelName | None
    reasoning_effort: ReasoningEffort | None
    sandbox: Literal["workspace-write", "read-only", "none"]
    approval_policy: Literal["never"]
    ephemeral: bool
    network_policy: Literal["disabled", "offline_test_no_credentials"]
    prompt_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    output_schema_path: str | None
    output_schema_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    handoff_path: str | None
    handoff_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None
    resume_thread_id: Annotated[str, Field(max_length=256)] | None
    test_id: Identifier | None
    argv: Annotated[tuple[str, ...], BeforeValidator(_freeze_sequence)]
    cwd: str | None
    timeout_seconds: Annotated[int, Field(ge=1, le=MAX_TIMEOUT_SECONDS)]
    transport_stdout_limit_bytes: Annotated[int, Field(ge=1)] | None
    transport_stderr_limit_bytes: Annotated[int, Field(ge=1)] | None
    max_stdout_bytes: Annotated[int, Field(ge=1, le=MAX_TEST_OUTPUT_BYTES)] | None
    max_stderr_bytes: Annotated[int, Field(ge=1, le=MAX_TEST_OUTPUT_BYTES)] | None
    removed_environment_variable_names: Annotated[
        tuple[str, ...], BeforeValidator(_freeze_sequence)
    ]
    skipped_after_action_id: Identifier | None
    started_at: RequiredString

    @model_validator(mode="after")
    def validate_kind_specific_evidence(self) -> PendingAction:
        if (
            tuple(
                sorted(
                    self.removed_environment_variable_names,
                    key=lambda item: (item.casefold(), item),
                )
            )
            != self.removed_environment_variable_names
            or len(set(self.removed_environment_variable_names))
            != len(self.removed_environment_variable_names)
            or any(
                not is_sensitive_name(name)
                for name in self.removed_environment_variable_names
            )
        ):
            raise ValueError(
                "pending removed environment-variable names are invalid"
            )
        if self.action_id != self.run_id:
            raise ValueError("pending action run_id must equal its deterministic action_id")
        if self.kind in {"worker", "auditor"}:
            expected_role = self.kind
            expected_sandbox = "workspace-write" if self.kind == "worker" else "read-only"
            if (
                self.role != expected_role
                or self.sandbox != expected_sandbox
                or self.network_policy != "disabled"
                or self.model is None
                or self.codex_executable is None
                or self.reasoning_effort is None
                or self.prompt_sha256 is None
                or self.output_schema_path is None
                or self.output_schema_sha256 is None
                or self.handoff_path is None
                or self.handoff_sha256 is None
                or self.test_id is not None
                or self.argv
                or self.cwd is not None
                or self.transport_stdout_limit_bytes is None
                or self.transport_stderr_limit_bytes is None
                or self.max_stdout_bytes is not None
                or self.max_stderr_bytes is not None
                or self.skipped_after_action_id is not None
            ):
                raise ValueError("pending Codex action evidence is incomplete or contradictory")
            if self.kind == "worker" and self.ephemeral:
                raise ValueError("worker actions must be persistent")
            if self.kind == "auditor" and (
                not self.ephemeral or self.resume_thread_id is not None
            ):
                raise ValueError("auditor actions must be fresh and ephemeral")
        elif (
            self.role != "fixed_test"
            or self.model is not None
            or self.codex_executable is not None
            or self.reasoning_effort is not None
            or self.sandbox != "none"
            or self.network_policy != "offline_test_no_credentials"
            or self.ephemeral
            or self.prompt_sha256 is not None
            or self.output_schema_path is not None
            or self.output_schema_sha256 is not None
            or self.handoff_path is not None
            or self.handoff_sha256 is not None
            or self.resume_thread_id is not None
            or self.test_id is None
            or not self.argv
            or self.cwd is None
            or self.transport_stdout_limit_bytes is not None
            or self.transport_stderr_limit_bytes is not None
            or self.max_stdout_bytes is None
            or self.max_stderr_bytes is None
        ):
            raise ValueError("pending fixed-test evidence is incomplete or contradictory")
        if self.approval_policy != "never":
            raise ValueError("pending actions must preserve the frozen safety policy")
        return self


class WorkflowState(BaseModel):
    """Strict durable snapshot used to drive, never infer, workflow transitions."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = 1
    substage_id: Identifier
    run_token: Identifier
    status: WorkflowStatus
    repair_round: Annotated[int, Field(ge=0)]
    max_repair_rounds: Annotated[int, Field(ge=0, le=10)]
    checkpoint_after: bool
    specification_path: RequiredString
    specification_sha256: str
    contract_sha256: str
    prompts_sha256: dict[str, str]
    workspace: RequiredString
    repository_root: RequiredString
    baseline_commit: str
    baseline_branch: str | None
    worker_thread_id: Annotated[str, Field(max_length=256)] | None
    latest_worker_action_id: str | None
    latest_audit_action_id: str | None
    latest_worker_result_path: str | None
    latest_audit_result_path: str | None
    latest_git_evidence_path: str | None
    latest_tests_path: str | None
    prior_audit_result_paths: Annotated[tuple[str, ...], BeforeValidator(_freeze_sequence)]
    completed_action_ids: Annotated[tuple[str, ...], BeforeValidator(_freeze_sequence)]
    pending_action: PendingAction | None
    repair_trigger: Literal["scope", "test", "audit", "human"] | None
    continuation_path: str | None
    continuation_sha256: str | None
    prompt_source_boundary: Literal[
        "initial_worker_prompt",
        "worker_repair_prompt",
        "auditor_prompt",
        "post_audit_terminal_decision",
    ] | None = None
    tests_passed: bool
    scope_compliant: bool
    contract_satisfied: bool
    pause_reason: Annotated[str, Field(max_length=MAX_STRUCTURED_STRING_BYTES)] | None
    summary: BoundedString
    artifact_directory: RequiredString
    journal_sequence: Annotated[int, Field(ge=0)]
    journal_hash: str
    started_at: RequiredString
    updated_at: RequiredString

    def to_result(self) -> WorkflowResult:
        return WorkflowResult(
            substage_id=self.substage_id,
            run_token=self.run_token,
            status=self.status,
            repair_round=self.repair_round,
            max_repair_rounds=self.max_repair_rounds,
            checkpoint_after=self.checkpoint_after,
            workspace=self.workspace,
            baseline_commit=self.baseline_commit,
            worker_thread_id=self.worker_thread_id,
            latest_worker_action_id=self.latest_worker_action_id,
            latest_audit_action_id=self.latest_audit_action_id,
            tests_passed=self.tests_passed,
            scope_compliant=self.scope_compliant,
            contract_satisfied=self.contract_satisfied,
            artifact_directory=self.artifact_directory,
            pause_reason=self.pause_reason,
            summary=self.summary,
            started_at=self.started_at,
            updated_at=self.updated_at,
        )


@dataclass(frozen=True)
class HumanFile:
    locator_path: Path
    path: Path
    content: bytes
    sha256: str


@dataclass(frozen=True)
class PreparedWorkflowTest:
    specification: WorkflowTest
    cwd: Path


@dataclass(frozen=True)
class PreparedSubstage:
    specification_locator_path: Path
    specification_path: Path
    specification_bytes: bytes
    specification_sha256: str
    specification: SubstageSpecification
    workspace: Path
    repository_root: Path
    baseline_commit: str
    baseline_branch: str | None
    contract: HumanFile
    worker_initial_prompt: HumanFile
    worker_repair_prompt: HumanFile
    auditor_prompt: HumanFile
    acceptance_tests: tuple[PreparedWorkflowTest, ...]

    def normalized_dict(self) -> dict[str, object]:
        value = self.specification.model_dump(mode="json")
        value.update(
            {
                "specification_path": str(self.specification_path),
                "workspace": str(self.workspace),
                "repository_root": str(self.repository_root),
                "contract_path": str(self.contract.path),
                "worker_initial_prompt_path": str(self.worker_initial_prompt.path),
                "worker_repair_prompt_path": str(self.worker_repair_prompt.path),
                "auditor_prompt_path": str(self.auditor_prompt.path),
                "acceptance_tests": [
                    {
                        **test.specification.model_dump(mode="json"),
                        "cwd": str(test.cwd),
                    }
                    for test in self.acceptance_tests
                ],
            }
        )
        return value


def load_substage_specification(
    path: Path,
    *,
    sensitive_values: Sequence[str] = (),
    require_clean: bool = True,
) -> PreparedSubstage:
    """Read once, resolve, and fully validate a Stage 2 substage specification."""
    specification_locator = _absolute_locator(path)
    specification_path = _resolve_regular_file(path, "substage specification")
    specification_bytes = _read_utf8_file(
        specification_path,
        "substage specification",
        limit=None,
    )
    try:
        parsed: Any = yaml.load(
            specification_bytes.decode("utf-8"), Loader=_UniqueKeySafeLoader
        )
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise WorkflowInputError(f"malformed substage YAML{location}: {problem}") from exc
    if not isinstance(parsed, dict):
        raise WorkflowInputError("substage specification root must be a YAML mapping")
    try:
        specification = SubstageSpecification.model_validate(parsed)
    except ValidationError as exc:
        details = "; ".join(_format_validation_error(error) for error in exc.errors())
        raise WorkflowInputError(f"substage specification validation failed: {details}") from exc

    resolved_parent = specification_path.parent
    locator_parent = specification_locator.parent
    workspace = _resolve_directory(
        resolved_parent, specification.workspace, "workspace"
    )
    repository_root, baseline_commit, baseline_branch, clean_status = _git_baseline(workspace)
    if require_clean and clean_status:
        raise WorkflowInputError("workspace must be clean, including untracked files")

    contract = _load_human_file(
        locator_parent,
        specification.contract_path,
        "contract",
        workspace,
        specification.protected_paths,
    )
    worker_initial = _load_human_file(
        locator_parent,
        specification.worker_initial_prompt_path,
        "worker initial prompt",
        workspace,
        specification.protected_paths,
    )
    worker_repair = _load_human_file(
        locator_parent,
        specification.worker_repair_prompt_path,
        "worker repair prompt",
        workspace,
        specification.protected_paths,
    )
    auditor = _load_human_file(
        locator_parent,
        specification.auditor_prompt_path,
        "auditor prompt",
        workspace,
        specification.protected_paths,
    )

    prepared_tests = tuple(
        PreparedWorkflowTest(
            specification=test,
            cwd=_resolve_test_cwd(resolved_parent, workspace, test.cwd),
        )
        for test in specification.acceptance_tests
    )

    structural = [
        str(path),
        str(specification_path),
        specification.substage_id,
        specification.title,
        str(workspace),
        str(repository_root),
        baseline_commit,
        baseline_branch or "detached",
        hashlib.sha256(specification_bytes).hexdigest(),
        specification.worker_model,
        specification.worker_reasoning_effort,
        str(specification.worker_timeout_seconds),
        specification.auditor_model,
        specification.auditor_reasoning_effort,
        str(specification.auditor_timeout_seconds),
        str(specification.max_repair_rounds),
        str(specification.checkpoint_after).lower(),
        "worker",
        "auditor",
        "workspace-write",
        "read-only",
        "never",
        "persistent",
        "ephemeral",
    ]
    for human_file in (contract, worker_initial, worker_repair, auditor):
        structural.extend((str(human_file.path), human_file.sha256))
    structural.extend(specification.allowed_paths)
    structural.extend(specification.protected_paths)
    for test in prepared_tests:
        structural.extend(
            (
                test.specification.id,
                str(test.cwd),
                str(test.specification.timeout_seconds),
                str(test.specification.max_stdout_bytes),
                str(test.specification.max_stderr_bytes),
                *test.specification.argv,
            )
        )
    if any(would_redact_text(item, sensitive_values) for item in structural):
        raise WorkflowInputError("substage contains a structural redaction collision")

    return PreparedSubstage(
        specification_locator_path=specification_locator,
        specification_path=specification_path,
        specification_bytes=specification_bytes,
        specification_sha256=hashlib.sha256(specification_bytes).hexdigest(),
        specification=specification,
        workspace=workspace,
        repository_root=repository_root,
        baseline_commit=baseline_commit,
        baseline_branch=baseline_branch,
        contract=contract,
        worker_initial_prompt=worker_initial,
        worker_repair_prompt=worker_repair,
        auditor_prompt=auditor,
        acceptance_tests=prepared_tests,
    )


def load_continuation_instruction(
    path: Path,
    *,
    sensitive_values: Sequence[str] = (),
    workspace: Path | None = None,
    protected_paths: Sequence[str] = (),
) -> HumanFile:
    """Read once and validate one exact human continuation instruction."""
    locator = _absolute_locator(path)
    _validate_supplied_human_locator(
        locator,
        workspace,
        protected_paths,
        "continuation instruction",
    )
    resolved = _resolve_regular_file(path, "continuation instruction")
    _validate_resolved_human_locator(
        locator,
        resolved,
        workspace,
        "continuation instruction",
    )
    content = _read_utf8_file(
        resolved, "continuation instruction", limit=MAX_HUMAN_FILE_BYTES
    )
    digest = hashlib.sha256(content).hexdigest()
    if any(
        would_redact_text(value, sensitive_values)
        for value in (str(path), str(resolved), digest)
    ):
        raise WorkflowInputError("continuation instruction has a structural redaction collision")
    return HumanFile(
        locator_path=locator,
        path=resolved,
        content=content,
        sha256=digest,
    )


def parse_worker_result(value: str | bytes) -> WorkerModelResult:
    """Parse one strict JSON worker result without consulting its prose."""
    return _parse_model_json(value, WorkerModelResult, "worker")


def parse_auditor_result(value: str | bytes) -> AuditorModelResult:
    """Parse one strict JSON auditor result without consulting its prose."""
    return _parse_model_json(value, AuditorModelResult, "auditor")


def path_matches_any(path: str, patterns: Sequence[str]) -> bool:
    """Match one normalized workspace path against normalized POSIX glob patterns."""
    import fnmatch

    normalized = normalize_relative_path(path)
    return any(fnmatch.fnmatchcase(normalized, pattern) for pattern in patterns)


ModelResultT = TypeVar("ModelResultT", WorkerModelResult, AuditorModelResult)


def _parse_model_json(
    value: str | bytes,
    model_type: type[ModelResultT],
    label: str,
) -> ModelResultT:
    try:
        text = value.decode("utf-8") if isinstance(value, bytes) else value
        parsed = json.loads(text, parse_constant=_reject_json_constant)
        if not isinstance(parsed, dict):
            raise ValueError("result root is not an object")
        return model_type.model_validate(parsed)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError) as exc:
        raise WorkflowInputError(f"{label} structured result is missing or invalid") from exc


def _resolve_regular_file(path: Path, label: str) -> Path:
    """Reject every symlink in the supplied lexical chain before resolution."""
    invalid_message = (
        f"{label} path is invalid and must not be a symbolic link or non-regular file"
    )
    try:
        supplied = os.fspath(path)
        if (
            supplied.endswith(os.sep)
            and supplied not in {os.sep, Path(path).anchor}
        ):
            raise OSError
        candidate = Path(path)
        if candidate.is_absolute():
            current = Path(candidate.anchor)
            components = candidate.parts[1:]
        else:
            current = Path.cwd()
            _inspect_directory_chain(current)
            components = candidate.parts
        final_status: os.stat_result | None = None
        for index, component in enumerate(components):
            if component in {"", "."}:
                continue
            if component == "..":
                current = current.parent
                final_status = current.lstat()
                if stat.S_ISLNK(final_status.st_mode):
                    raise OSError
                continue
            current = current / component
            final_status = current.lstat()
            if stat.S_ISLNK(final_status.st_mode):
                raise OSError
            if index < len(components) - 1:
                if not stat.S_ISDIR(final_status.st_mode):
                    raise OSError
            elif not stat.S_ISREG(final_status.st_mode):
                raise OSError
        if final_status is None:
            final_status = current.lstat()
        if not stat.S_ISREG(final_status.st_mode):
            raise OSError
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise WorkflowInputError(invalid_message) from exc
    locator = _absolute_locator(path)
    try:
        resolved = locator.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowInputError(invalid_message) from exc
    try:
        resolved_status = resolved.lstat()
    except OSError as exc:
        raise WorkflowInputError(invalid_message) from exc
    if (
        resolved != locator
        or stat.S_ISLNK(resolved_status.st_mode)
        or not stat.S_ISREG(resolved_status.st_mode)
        or (resolved_status.st_dev, resolved_status.st_ino)
        != (final_status.st_dev, final_status.st_ino)
    ):
        raise WorkflowInputError(invalid_message)
    return resolved


def _inspect_directory_chain(path: Path) -> None:
    """Inspect one absolute trusted base from its anchor without following links."""
    if not path.is_absolute():
        raise OSError
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        status = current.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise OSError


def _resolve_directory(parent: Path, value: str, label: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = parent / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowInputError(f"{label} path could not be resolved") from exc
    if not resolved.is_dir():
        raise WorkflowInputError(f"{label} is not a directory")
    return resolved


def _load_human_file(
    parent: Path,
    value: str,
    label: str,
    workspace: Path,
    protected_paths: Sequence[str],
) -> HumanFile:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = parent / candidate
    locator = _absolute_locator(candidate)
    _validate_supplied_human_locator(
        locator,
        workspace,
        protected_paths,
        label,
    )
    path = _resolve_regular_file(candidate, label)
    _validate_resolved_human_locator(locator, path, workspace, label)
    content = _read_utf8_file(path, label, limit=MAX_HUMAN_FILE_BYTES)
    return HumanFile(
        locator_path=locator,
        path=path,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _absolute_locator(path: Path) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError) as exc:
        raise WorkflowInputError("input path could not be normalized") from exc


def _validate_supplied_human_locator(
    locator: Path,
    workspace: Path | None,
    protected_paths: Sequence[str],
    label: str,
) -> None:
    if workspace is None:
        return
    supplied_relative = _relative_to(locator, workspace)
    if supplied_relative is None:
        return
    try:
        normalized = normalize_relative_path(supplied_relative)
    except ValueError as exc:
        raise WorkflowInputError(f"{label} locator is invalid") from exc
    if not path_matches_any(normalized, protected_paths):
        raise WorkflowInputError(
            "contract, prompt, and continuation files inside the workspace "
            "must match protected_paths"
        )


def _validate_resolved_human_locator(
    locator: Path,
    resolved: Path,
    workspace: Path | None,
    label: str,
) -> None:
    if (
        workspace is not None
        and _relative_to(locator, workspace) is not None
        and _relative_to(resolved, workspace) is None
    ):
        raise WorkflowInputError(f"{label} locator is invalid")


def _read_utf8_file(path: Path, label: str, limit: int | None) -> bytes:
    try:
        size = path.stat().st_size
        if limit is not None and size > limit:
            raise WorkflowInputError(f"{label} exceeds the {limit}-byte limit")
        content = path.read_bytes()
    except WorkflowInputError:
        raise
    except OSError as exc:
        raise WorkflowInputError(f"{label} could not be read") from exc
    if limit is not None and len(content) > limit:
        raise WorkflowInputError(f"{label} exceeds the {limit}-byte limit")
    if not content:
        raise WorkflowInputError(f"{label} must not be empty")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkflowInputError(f"{label} is not valid UTF-8") from exc
    if not text.strip():
        raise WorkflowInputError(f"{label} must not be empty after trimming")
    return content


def _resolve_test_cwd(parent: Path, workspace: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = parent / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowInputError("acceptance-test cwd could not be resolved") from exc
    if not resolved.is_dir() or _relative_to(resolved, workspace) is None:
        raise WorkflowInputError("acceptance-test cwd must be a directory inside the workspace")
    return resolved


def _git_baseline(workspace: Path) -> tuple[Path, str, str | None, str]:
    git = "git"
    environment = {
        name: value for name, value in os.environ.items() if not is_sensitive_name(name)
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"

    def run(arguments: Sequence[str]) -> str:
        try:
            completed = subprocess.run(
                [git, "--no-pager", "--no-optional-locks", "-C", str(workspace), *arguments],
                capture_output=True,
                check=False,
                env=environment,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise WorkflowDependencyError("Git executable is required") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkflowInputError("Git baseline validation could not be completed") from exc
        if completed.returncode != 0:
            raise WorkflowInputError("workspace is not a usable Git worktree")
        try:
            return completed.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkflowInputError("Git baseline output is not valid UTF-8") from exc

    try:
        repository_root = Path(run(("rev-parse", "--show-toplevel")).strip()).resolve(
            strict=True
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkflowInputError("Git repository root could not be resolved") from exc
    head = run(("rev-parse", "--verify", "HEAD")).strip()
    branch_value: str | None = None
    try:
        completed = subprocess.run(
            [
                git,
                "--no-pager",
                "--no-optional-locks",
                "-C",
                str(workspace),
                "symbolic-ref",
                "--quiet",
                "--short",
                "HEAD",
            ],
            capture_output=True,
            check=False,
            env=environment,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise WorkflowDependencyError("Git executable is required") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkflowInputError("Git branch validation could not be completed") from exc
    if completed.returncode not in {0, 1}:
        raise WorkflowInputError("Git branch validation could not be completed")
    if completed.returncode == 0:
        try:
            branch_value = completed.stdout.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise WorkflowInputError("Git branch output is not valid UTF-8") from exc
    status = run(("status", "--porcelain=v1", "--untracked-files=all"))
    return repository_root, head, branch_value, status


def _relative_to(path: Path, parent: Path) -> str | None:
    try:
        return path.relative_to(parent).as_posix()
    except ValueError:
        return None


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")
