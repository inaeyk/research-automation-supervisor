"""Strict proof models for Stage 2 action and journal integrity."""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from research_automation_supervisor.codex_models import (
    CodexRunResult,
    ModelName,
    ReasoningEffort,
    Role,
    RunStatus,
)
from research_automation_supervisor.errors import WorkflowStateError
from research_automation_supervisor.redaction import is_sensitive_name
from research_automation_supervisor.test_runner import TestAttemptResult
from research_automation_supervisor.workflow_models import (
    AuditorModelResult,
    PendingAction,
    WorkerModelResult,
    WorkflowStatus,
    _freeze_sequence,
)

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
StringTuple = Annotated[tuple[str, ...], BeforeValidator(_freeze_sequence)]
ActionKind = Literal["worker", "auditor", "test"]
JournalEventType = Literal["transition", "evidence", "action_intent", "action_completion"]
ModelT = TypeVar("ModelT", bound=BaseModel)

STAGE1_CORE_ARTIFACT_NAMES = frozenset(
    {
        "request.normalized.json",
        "prompt.sha256",
        "events.jsonl",
        "stderr.log",
        "final-message.md",
        "metadata.json",
        "result.json",
    }
)
STAGE2_STAGE1_ARTIFACT_NAMES = STAGE1_CORE_ARTIFACT_NAMES | {
    "stage2-completion.json"
}


class NormalizedRolePolicy(BaseModel):
    """Strict policy recorded in a normalized Stage 1 request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    sandbox: Literal["read-only", "workspace-write"]
    approval: Literal["never"]
    ephemeral: bool


class NormalizedCodexRequest(BaseModel):
    """Strict normalized request artifact written before the Codex launch."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    run_id: str
    role: Role
    workspace: str
    prompt_path: str
    model: ModelName
    reasoning_effort: ReasoningEffort
    timeout_seconds: Annotated[int, Field(ge=30, le=14_400)]
    policy: NormalizedRolePolicy
    skip_git_repo_check: bool = False


class CodexMetadata(BaseModel):
    """Exact Stage 1 metadata schema consumed by Stage 2 proof validation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    package_version: str
    run_id: str
    role: Role
    workspace: str
    prompt_path: str
    prompt_sha256: Sha256
    prompt_byte_count: Annotated[int, Field(ge=1)]
    model: ModelName
    reasoning_effort: ReasoningEffort
    timeout_seconds: Annotated[int, Field(ge=30, le=14_400)]
    sandbox: Literal["read-only", "workspace-write"]
    approval_policy: Literal["never"]
    ephemeral: bool
    command: StringTuple
    removed_environment_variable_names: StringTuple
    started_at: str
    ended_at: str
    duration_seconds: Annotated[float, Field(ge=0)]
    artifact_directory: str
    codex_executable: str
    codex_version: str | None
    process_launched: bool
    launch_error_present: bool
    stdin_error: bool
    process_exit_code: int | None
    terminating_signal: Annotated[int, Field(gt=0)] | None
    termination_reason: Literal["timeout", "output_limit"] | None
    valid_event_count: Annotated[int, Field(ge=0)]
    malformed_event_count: Annotated[int, Field(ge=0)]
    malformed_event_sha256: Annotated[
        tuple[Sha256, ...], BeforeValidator(_freeze_sequence)
    ]
    stdout_byte_count: Annotated[int, Field(ge=0)]
    stderr_byte_count: Annotated[int, Field(ge=0)]
    stdout_limit_bytes: Annotated[int, Field(ge=1)]
    stderr_limit_bytes: Annotated[int, Field(ge=1)]
    final_message_present: bool
    permission_evidence: bool
    confidentiality_violation_detected: bool = Field(
        default=False,
        exclude_if=lambda value: not value,
    )
    output_limit_stream: Literal["stdout", "stderr"] | None
    thread_id: str | None
    session_id: str | None
    thread_started_ids: StringTuple
    resume_thread_id: str | None
    output_schema_path: str | None
    output_schema_sha256: Sha256 | None
    events_sha256: Sha256
    stderr_sha256: Sha256
    final_message_sha256: Sha256

    @model_validator(mode="after")
    def validate_process_fields(self) -> CodexMetadata:
        if self.process_exit_code is not None and self.terminating_signal is not None:
            raise ValueError("Codex exit code and signal are mutually exclusive")
        if len(self.malformed_event_sha256) != self.malformed_event_count:
            raise ValueError("malformed-event hashes do not match the recorded count")
        if self.termination_reason == "output_limit" and self.output_limit_stream is None:
            raise ValueError("output-limit termination must identify the bounded stream")
        if self.termination_reason != "output_limit" and self.output_limit_stream is not None:
            raise ValueError("output-limit stream contradicts the termination reason")
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
            raise ValueError("removed environment-variable names are invalid")
        return self


class PromptHandoff(BaseModel):
    """Strict engine-owned prompt assembly record."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    kind: Literal[
        "initial_worker",
        "fixed_test_or_scope_repair",
        "audit_repair",
        "human_continuation",
        "auditor",
    ]
    source_path: str
    source_sha256: Sha256
    contract_sha256: Sha256
    evidence_sha256: dict[str, Sha256]
    rendered_prompt_sha256: Sha256
    rendered_prompt_byte_count: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def validate_evidence_hash_fields(self) -> PromptHandoff:
        if set(self.evidence_sha256) != {
            "evidence",
            "output_schema",
            "reporting_instruction",
        }:
            raise ValueError("prompt handoff evidence hashes are incomplete")
        return self


class Stage2CompletionManifest(BaseModel):
    """Stage 1 finalization marker written last for a Stage 2 Codex action."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    run_id: str
    role: Role
    artifact_directory: str
    prompt_sha256: Sha256
    output_schema_path: str
    output_schema_sha256: Sha256
    result_status: RunStatus
    completed_at: str
    artifact_hashes: dict[str, Sha256]


class CodexActionRecord(BaseModel):
    """Complete Stage 2 record for one validated worker or auditor action."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    action_id: str
    kind: Literal["worker", "auditor"]
    repair_round: Annotated[int, Field(ge=0)]
    complete: Literal[True]
    run_id: str
    stage1_artifact_directory: str
    artifact_hashes: dict[str, Sha256]
    handoff_path: str
    handoff_sha256: Sha256
    output_schema_path: str
    output_schema_sha256: Sha256
    adapter_result: CodexRunResult
    thread_started_ids: StringTuple
    structured_result_valid: bool
    structured_result_path: str | None
    structured_result_sha256: Sha256 | None

    @model_validator(mode="after")
    def validate_structured_locator(self) -> CodexActionRecord:
        if self.structured_result_valid != (self.structured_result_path is not None):
            raise ValueError("structured-result validity and locator contradict")
        if self.structured_result_valid != (self.structured_result_sha256 is not None):
            raise ValueError("structured-result validity and hash contradict")
        return self


class TestActionRecord(BaseModel):
    """Complete Stage 2 record for one validated fixed-test action."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    action_id: str
    kind: Literal["test"]
    repair_round: Annotated[int, Field(ge=0)]
    complete: Literal[True]
    result_path: str
    result_sha256: Sha256
    artifact_hashes: dict[str, Sha256]
    result: TestAttemptResult


class JournalEntry(BaseModel):
    """Exact journal entry schema; semantic validation is performed over the chain."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1]
    sequence: Annotated[int, Field(ge=1)]
    event_type: JournalEventType
    previous_state: WorkflowStatus | None
    new_state: WorkflowStatus
    action_id: str | None
    action_kind: ActionKind | None
    timestamp: str
    reason: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_]{0,127}$")]
    artifact_hashes: dict[str, Sha256]
    state_updates: dict[str, Any]
    previous_hash: Sha256
    entry_hash: Sha256


@dataclass(frozen=True)
class CodexArtifactProof:
    """Fully checked Stage 1 evidence used by normal completion and recovery."""

    adapter_result: CodexRunResult
    metadata: CodexMetadata
    request: NormalizedCodexRequest
    handoff: PromptHandoff
    thread_started_ids: tuple[str, ...]
    structured_result: WorkerModelResult | AuditorModelResult | None
    artifact_hashes: dict[str, str]


def sha256_regular_file(path: Path) -> str:
    """Hash one exact regular, non-symlink file."""
    try:
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise WorkflowStateError("durable artifact is not an exact regular file")
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except WorkflowStateError:
        raise
    except OSError as exc:
        raise WorkflowStateError("durable artifact is missing or unreadable") from exc


def verify_hash_mapping(mapping: Mapping[str, str]) -> None:
    """Recompute every locator in a durable artifact-hash mapping."""
    for locator, expected in mapping.items():
        path = Path(locator)
        if not path.is_absolute() or str(path) != locator:
            raise WorkflowStateError("durable artifact locator is not exact and absolute")
        if sha256_regular_file(path) != expected:
            raise WorkflowStateError("durable artifact hash does not match")


def parse_journal_entry(value: object) -> JournalEntry:
    """Parse one strict journal entry without accepting coercion or extra fields."""
    try:
        return JournalEntry.model_validate(value)
    except ValidationError as exc:
        raise WorkflowStateError("workflow journal entry schema is invalid") from exc


def parse_action_record(value: object) -> CodexActionRecord | TestActionRecord:
    """Parse one strict action record by its frozen action kind."""
    if not isinstance(value, dict):
        raise WorkflowStateError("action record is not an object")
    model: type[CodexActionRecord] | type[TestActionRecord]
    model = TestActionRecord if value.get("kind") == "test" else CodexActionRecord
    try:
        return model.model_validate(value)
    except ValidationError as exc:
        raise WorkflowStateError("action record schema is invalid") from exc


def verify_codex_artifacts(
    pending: PendingAction,
    *,
    known_worker_thread_id: str | None,
) -> CodexArtifactProof:
    """Prove a worker/auditor action from its intent and complete Stage 1 artifacts."""
    if pending.kind not in {"worker", "auditor"}:
        raise WorkflowStateError("Codex proof received a non-Codex intent")
    artifact_directory = Path(pending.artifact_path)
    _require_exact_directory(artifact_directory, STAGE2_STAGE1_ARTIFACT_NAMES)
    completion = _model_from_json(
        artifact_directory / "stage2-completion.json",
        Stage2CompletionManifest,
        "Stage 2 completion manifest",
    )
    expected_core_paths = {
        str(artifact_directory / name) for name in STAGE1_CORE_ARTIFACT_NAMES
    }
    if set(completion.artifact_hashes) != expected_core_paths:
        raise WorkflowStateError("Stage 2 completion manifest artifact set is incomplete")
    verify_hash_mapping(completion.artifact_hashes)
    request = _model_from_json(
        artifact_directory / "request.normalized.json",
        NormalizedCodexRequest,
        "normalized Codex request",
    )
    metadata = _model_from_json(
        artifact_directory / "metadata.json",
        CodexMetadata,
        "Codex metadata",
    )
    adapter_result = _model_from_json(
        artifact_directory / "result.json",
        CodexRunResult,
        "Codex normalized result",
    )
    if pending.handoff_path is None:
        raise WorkflowStateError("Codex intent has no prompt handoff")
    handoff = _model_from_json(
        Path(pending.handoff_path),
        PromptHandoff,
        "prompt handoff",
    )
    _verify_codex_identity(pending, request, metadata, adapter_result, handoff)
    if (
        completion.run_id != pending.run_id
        or completion.role != pending.role
        or completion.artifact_directory != pending.artifact_path
        or completion.prompt_sha256 != pending.prompt_sha256
        or completion.output_schema_path != pending.output_schema_path
        or completion.output_schema_sha256 != pending.output_schema_sha256
        or completion.result_status != adapter_result.status
        or completion.completed_at != adapter_result.ended_at
    ):
        raise WorkflowStateError("Stage 2 completion manifest contradicts its action intent")

    prompt_hash_text = _read_exact_bytes(artifact_directory / "prompt.sha256")
    if prompt_hash_text != f"{pending.prompt_sha256}\n".encode("ascii"):
        raise WorkflowStateError("Codex prompt hash artifact does not match the intent")

    events_path = artifact_directory / "events.jsonl"
    events_bytes = _read_exact_bytes(events_path)
    events, thread_ids, first_thread_id, first_session_id = _parse_events(events_bytes)
    if len(events) != metadata.valid_event_count:
        raise WorkflowStateError("Codex event count contradicts metadata")
    if metadata.thread_started_ids != thread_ids:
        raise WorkflowStateError("Codex thread evidence was not derived from events")
    if metadata.thread_id != first_thread_id or metadata.session_id != first_session_id:
        raise WorkflowStateError("Codex identifier metadata contradicts events")
    if adapter_result.event_count != len(events):
        raise WorkflowStateError("Codex normalized event count contradicts the event artifact")
    if adapter_result.malformed_event_count != metadata.malformed_event_count:
        raise WorkflowStateError("Codex malformed-event counts contradict")

    stderr_path = artifact_directory / "stderr.log"
    final_path = artifact_directory / "final-message.md"
    final_bytes = _read_exact_bytes(final_path)
    if metadata.events_sha256 != hashlib.sha256(events_bytes).hexdigest():
        raise WorkflowStateError("Codex events hash contradicts metadata")
    if metadata.stderr_sha256 != sha256_regular_file(stderr_path):
        raise WorkflowStateError("Codex stderr hash contradicts metadata")
    if metadata.final_message_sha256 != hashlib.sha256(final_bytes).hexdigest():
        raise WorkflowStateError("Codex final-message hash contradicts metadata")
    if metadata.final_message_present != bool(final_bytes.decode("utf-8").strip()):
        raise WorkflowStateError("Codex final-message presence contradicts its artifact")
    if adapter_result.final_message_present != metadata.final_message_present:
        raise WorkflowStateError("Codex result and metadata disagree about the final message")

    _verify_codex_process_result(metadata, adapter_result)
    _verify_codex_command(pending, metadata)
    _verify_time_agreement(
        metadata.started_at,
        metadata.ended_at,
        metadata.duration_seconds,
        "Codex metadata",
    )
    if _parse_timestamp(metadata.started_at, "Codex metadata") < _parse_timestamp(
        pending.started_at,
        "Codex action intent",
    ):
        raise WorkflowStateError("Codex process started before its durable action intent")
    if (
        adapter_result.started_at != metadata.started_at
        or adapter_result.ended_at != metadata.ended_at
        or adapter_result.duration_seconds != metadata.duration_seconds
    ):
        raise WorkflowStateError("Codex result timestamps contradict metadata")

    structured: WorkerModelResult | AuditorModelResult | None = None
    if adapter_result.status == "succeeded":
        try:
            parsed = json.loads(final_bytes.decode("utf-8"), parse_constant=_reject_constant)
            model = WorkerModelResult if pending.kind == "worker" else AuditorModelResult
            structured = model.model_validate(parsed)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError):
            structured = None
        if (
            pending.kind == "auditor"
            and known_worker_thread_id is not None
            and known_worker_thread_id in thread_ids
        ):
            raise WorkflowStateError("fresh auditor substituted the persistent worker session")

    artifact_hashes = {
        str(artifact_directory / name): sha256_regular_file(artifact_directory / name)
        for name in sorted(STAGE2_STAGE1_ARTIFACT_NAMES)
    }
    artifact_hashes[pending.handoff_path] = sha256_regular_file(Path(pending.handoff_path))
    if pending.output_schema_path is None:
        raise WorkflowStateError("Codex intent has no output schema")
    artifact_hashes[pending.output_schema_path] = sha256_regular_file(
        Path(pending.output_schema_path)
    )
    return CodexArtifactProof(
        adapter_result=adapter_result,
        metadata=metadata,
        request=request,
        handoff=handoff,
        thread_started_ids=thread_ids,
        structured_result=structured,
        artifact_hashes=artifact_hashes,
    )


def verify_codex_action_record(
    record: CodexActionRecord,
    pending: PendingAction,
    proof: CodexArtifactProof,
) -> None:
    """Validate a Stage 2 Codex action record against intent and Stage 1 proof."""
    if (
        record.action_id != pending.action_id
        or record.kind != pending.kind
        or record.repair_round != pending.repair_round
        or record.run_id != pending.run_id
        or record.stage1_artifact_directory != pending.artifact_path
        or record.handoff_path != pending.handoff_path
        or record.handoff_sha256 != pending.handoff_sha256
        or record.output_schema_path != pending.output_schema_path
        or record.output_schema_sha256 != pending.output_schema_sha256
        or record.adapter_result != proof.adapter_result
        or record.thread_started_ids != proof.thread_started_ids
    ):
        raise WorkflowStateError("Codex action record contradicts its prior intent")
    if record.artifact_hashes != proof.artifact_hashes:
        raise WorkflowStateError("Codex action record artifact mapping is incomplete")
    verify_hash_mapping(record.artifact_hashes)
    expected_valid = proof.structured_result is not None
    if record.structured_result_valid != expected_valid:
        raise WorkflowStateError("Codex action record structured-result status contradicts proof")
    if expected_valid:
        if record.structured_result_path is None or record.structured_result_sha256 is None:
            raise WorkflowStateError("Codex action record structured result is incomplete")
        expected_path = _structured_result_path(pending)
        if record.structured_result_path != str(expected_path):
            raise WorkflowStateError("Codex structured-result locator is not exact")
        if sha256_regular_file(expected_path) != record.structured_result_sha256:
            raise WorkflowStateError("Codex structured-result hash does not match")
        model = WorkerModelResult if pending.kind == "worker" else AuditorModelResult
        persisted = _model_from_json(expected_path, model, "structured Codex result")
        if persisted != proof.structured_result:
            raise WorkflowStateError("Codex structured result contradicts the final message")
    elif record.structured_result_path is not None or record.structured_result_sha256 is not None:
        raise WorkflowStateError("invalid Codex result unexpectedly has a structured locator")


def verify_test_artifacts(pending: PendingAction) -> TestAttemptResult:
    """Prove one fixed-test result from its exact intent and bounded durable logs."""
    if pending.kind != "test":
        raise WorkflowStateError("fixed-test proof received a non-test intent")
    directory = Path(pending.artifact_path)
    result_path = directory / "result.json"
    result = _model_from_json(result_path, TestAttemptResult, "fixed-test result")
    _require_exact_directory(
        directory,
        frozenset({"result.json", "stdout.log", "stderr.log"}),
    )
    if (
        result.action_id != pending.action_id
        or result.test_id != pending.test_id
        or result.argv != pending.argv
        or result.cwd != pending.cwd
        or result.timeout_seconds != pending.timeout_seconds
        or result.max_stdout_bytes != pending.max_stdout_bytes
        or result.max_stderr_bytes != pending.max_stderr_bytes
    ):
        raise WorkflowStateError("fixed-test result contradicts its exact intent")
    if result.status == "skipped":
        if pending.skipped_after_action_id is None:
            raise WorkflowStateError("fixed-test skip has no recorded first failure")
    elif pending.skipped_after_action_id is not None:
        raise WorkflowStateError("launched fixed test incorrectly claims a skip predecessor")
    if result.status != "skipped" and result.removed_environment_variable_names != (
        pending.removed_environment_variable_names
    ):
        raise WorkflowStateError("fixed-test environment filtering contradicts its intent")
    stdout = directory / "stdout.log"
    stderr = directory / "stderr.log"
    if result.stdout_artifact != str(stdout) or result.stderr_artifact != str(stderr):
        raise WorkflowStateError("fixed-test log locators are not exact")
    stdout_bytes = _read_exact_bytes(stdout)
    stderr_bytes = _read_exact_bytes(stderr)
    if (
        len(stdout_bytes) != result.stdout_stored_byte_count
        or len(stderr_bytes) != result.stderr_stored_byte_count
        or hashlib.sha256(stdout_bytes).hexdigest() != result.stdout_sha256
        or hashlib.sha256(stderr_bytes).hexdigest() != result.stderr_sha256
    ):
        raise WorkflowStateError("fixed-test bounded log hashes or byte counts do not match")
    if result.status != "skipped":
        if result.started_at is None or result.ended_at is None:
            raise WorkflowStateError("launched fixed test has no timestamps")
        if _parse_timestamp(
            result.started_at,
            "fixed-test result",
        ) < _parse_timestamp(pending.started_at, "fixed-test action intent"):
            raise WorkflowStateError("fixed test started before its durable action intent")
        _verify_time_agreement(
            result.started_at,
            result.ended_at,
            result.duration_seconds,
            "fixed-test result",
        )
    return result


def verify_test_action_record(
    record: TestActionRecord,
    pending: PendingAction,
    result: TestAttemptResult,
) -> None:
    """Validate one Stage 2 fixed-test record and every referenced file."""
    result_path = Path(pending.artifact_path) / "result.json"
    expected_hashes = {str(result_path): sha256_regular_file(result_path)}
    for name in ("stdout.log", "stderr.log"):
        path = Path(pending.artifact_path) / name
        expected_hashes[str(path)] = sha256_regular_file(path)
    if (
        record.action_id != pending.action_id
        or record.repair_round != pending.repair_round
        or record.result_path != str(result_path)
        or record.result_sha256 != expected_hashes[str(result_path)]
        or record.artifact_hashes != expected_hashes
        or record.result != result
    ):
        raise WorkflowStateError("fixed-test action record contradicts its proof")
    verify_hash_mapping(record.artifact_hashes)


def action_record_artifact_hashes(
    record: CodexActionRecord | TestActionRecord,
) -> dict[str, str]:
    """Return the complete recursive hash mapping recorded by an action."""
    return dict(record.artifact_hashes)


def _verify_codex_identity(
    pending: PendingAction,
    request: NormalizedCodexRequest,
    metadata: CodexMetadata,
    result: CodexRunResult,
    handoff: PromptHandoff,
) -> None:
    if (
        request.run_id != pending.run_id
        or request.role != pending.role
        or request.workspace != pending.workspace
        or request.model != pending.model
        or request.reasoning_effort != pending.reasoning_effort
        or request.timeout_seconds != pending.timeout_seconds
        or request.policy.sandbox != pending.sandbox
        or request.policy.approval != pending.approval_policy
        or request.policy.ephemeral != pending.ephemeral
    ):
        raise WorkflowStateError("normalized Codex request contradicts its prior intent")
    if (
        metadata.run_id != pending.run_id
        or metadata.role != pending.role
        or metadata.workspace != pending.workspace
        or metadata.model != pending.model
        or metadata.reasoning_effort != pending.reasoning_effort
        or metadata.timeout_seconds != pending.timeout_seconds
        or metadata.sandbox != pending.sandbox
        or metadata.approval_policy != pending.approval_policy
        or metadata.ephemeral != pending.ephemeral
        or metadata.prompt_sha256 != pending.prompt_sha256
        or metadata.artifact_directory != pending.artifact_path
        or metadata.codex_executable != pending.codex_executable
        or metadata.resume_thread_id != pending.resume_thread_id
        or metadata.output_schema_path != pending.output_schema_path
        or metadata.output_schema_sha256 != pending.output_schema_sha256
        or metadata.stdout_limit_bytes != pending.transport_stdout_limit_bytes
        or metadata.stderr_limit_bytes != pending.transport_stderr_limit_bytes
        or metadata.removed_environment_variable_names
        != pending.removed_environment_variable_names
    ):
        raise WorkflowStateError("Codex metadata contradicts its prior intent")
    if (
        result.run_id != pending.run_id
        or result.artifact_directory != pending.artifact_path
    ):
        raise WorkflowStateError("Codex normalized result contradicts its prior intent")
    if (
        handoff.rendered_prompt_sha256 != pending.prompt_sha256
        or handoff.rendered_prompt_byte_count != metadata.prompt_byte_count
        or handoff.source_path != request.prompt_path
        or pending.handoff_sha256 != sha256_regular_file(Path(cast(str, pending.handoff_path)))
        or pending.output_schema_sha256
        != sha256_regular_file(Path(cast(str, pending.output_schema_path)))
        or handoff.evidence_sha256.get("output_schema") != pending.output_schema_sha256
    ):
        raise WorkflowStateError("prompt handoff or output schema contradicts the action intent")
    if pending.kind == "worker" and handoff.kind == "auditor":
        raise WorkflowStateError("worker action received an auditor handoff")
    if pending.kind == "auditor" and handoff.kind != "auditor":
        raise WorkflowStateError("auditor action did not receive an auditor handoff")


def _verify_codex_process_result(
    metadata: CodexMetadata,
    result: CodexRunResult,
) -> None:
    if (
        result.event_count != metadata.valid_event_count
        or result.malformed_event_count != metadata.malformed_event_count
        or result.final_message_present != metadata.final_message_present
        or result.permission_evidence != metadata.permission_evidence
        or result.confidentiality_violation_detected
        != metadata.confidentiality_violation_detected
    ):
        raise WorkflowStateError("Codex normalized result contradicts metadata")
    output_breached = (
        metadata.stdout_byte_count > metadata.stdout_limit_bytes
        or metadata.stderr_byte_count > metadata.stderr_limit_bytes
    )
    if result.status == "succeeded":
        valid = (
            metadata.process_exit_code == 0
            and metadata.process_launched
            and not metadata.launch_error_present
            and not metadata.stdin_error
            and metadata.terminating_signal is None
            and metadata.termination_reason is None
            and metadata.malformed_event_count == 0
            and metadata.final_message_present
            and not result.permission_evidence
            and result.error is None
            and not output_breached
            and not result.confidentiality_violation_detected
        )
    elif result.status == "timed_out":
        valid = metadata.termination_reason == "timeout"
    elif result.status == "output_limit_exceeded":
        selected_count = (
            metadata.stdout_byte_count
            if metadata.output_limit_stream == "stdout"
            else metadata.stderr_byte_count
        )
        selected_limit = (
            metadata.stdout_limit_bytes
            if metadata.output_limit_stream == "stdout"
            else metadata.stderr_limit_bytes
        )
        valid = (
            metadata.termination_reason == "output_limit"
            and selected_count > selected_limit
        )
    elif result.status == "malformed_event_stream":
        valid = metadata.malformed_event_count > 0
    elif result.status == "missing_final_message":
        valid = not metadata.final_message_present
    elif result.status == "process_failed":
        valid = (
            metadata.process_exit_code not in {None, 0}
            or metadata.terminating_signal is not None
            or metadata.stdin_error
            or metadata.confidentiality_violation_detected
        )
    elif result.status == "launch_failed":
        valid = (
            (not metadata.process_launched or metadata.launch_error_present)
            and
            metadata.process_exit_code is None
            and metadata.terminating_signal is None
            and metadata.termination_reason is None
        )
    else:
        valid = result.status == "permission_blocked" and result.permission_evidence
    if (
        result.status not in {"timed_out", "output_limit_exceeded"}
        and output_breached
    ):
        valid = False
    if not valid:
        raise WorkflowStateError("Codex process status and normalized result contradict")


def _verify_codex_command(pending: PendingAction, metadata: CodexMetadata) -> None:
    executable = metadata.codex_executable
    common_configuration = [
        "-c",
        f"model_reasoning_effort={pending.reasoning_effort}",
        "-c",
        'web_search="disabled"',
        "-c",
        "sandbox_workspace_write.network_access=false",
        "-c",
        "features.skill_mcp_dependency_install=false",
    ]
    if pending.resume_thread_id is None:
        expected = [
            executable,
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--output-last-message",
            "<FINAL_MESSAGE_TEMP>",
            "--model",
            cast(str, pending.model),
            *common_configuration,
            "--sandbox",
            pending.sandbox,
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--cd",
            pending.workspace,
        ]
        if pending.ephemeral:
            expected.append("--ephemeral")
    else:
        expected = [
            executable,
            "--ask-for-approval",
            "never",
            "--sandbox",
            pending.sandbox,
            "--cd",
            pending.workspace,
            "exec",
            "resume",
            pending.resume_thread_id,
            "--json",
            "--output-last-message",
            "<FINAL_MESSAGE_TEMP>",
            "--model",
            cast(str, pending.model),
            *common_configuration,
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
        ]
    expected.extend(
        ["--output-schema", cast(str, pending.output_schema_path), "<PROMPT_FROM_STDIN>"]
    )
    if metadata.command != tuple(expected):
        raise WorkflowStateError("Codex command does not preserve the exact frozen policy")
    if "--last" in metadata.command or "--all" in metadata.command:
        raise WorkflowStateError("Codex command used forbidden recency-based resume")


def _parse_events(
    content: bytes,
) -> tuple[tuple[dict[str, Any], ...], tuple[str, ...], str | None, str | None]:
    if content and not content.endswith(b"\n"):
        raise WorkflowStateError("Codex event artifact is truncated")
    events: list[dict[str, Any]] = []
    thread_ids: list[str] = []
    first_thread_id: str | None = None
    first_session_id: str | None = None
    identifier_recorded = False
    for raw_line in content.splitlines():
        if not raw_line:
            raise WorkflowStateError("Codex event artifact contains an empty record")
        try:
            value = json.loads(raw_line.decode("ascii"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise WorkflowStateError("Codex event artifact is malformed or truncated") from exc
        if not isinstance(value, dict):
            raise WorkflowStateError("Codex event record is not an object")
        canonical = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        if canonical != raw_line:
            raise WorkflowStateError("Codex event artifact is not canonical")
        events.append(cast(dict[str, Any], value))
        direct_thread = value.get("thread_id")
        direct_session = value.get("session_id")
        if (
            not identifier_recorded
            and isinstance(direct_thread, str)
            and direct_thread.strip()
        ):
            first_thread_id = direct_thread.strip()
            identifier_recorded = True
        elif (
            not identifier_recorded
            and isinstance(direct_session, str)
            and direct_session.strip()
        ):
            first_session_id = direct_session.strip()
            identifier_recorded = True
        event_type = value.get("type")
        started: str | None = None
        if isinstance(event_type, str) and event_type.casefold() == "thread.started":
            if isinstance(direct_thread, str) and direct_thread.strip():
                started = direct_thread.strip()
            else:
                nested = value.get("thread")
                if isinstance(nested, dict):
                    nested_id = nested.get("id")
                    if isinstance(nested_id, str) and nested_id.strip():
                        started = nested_id.strip()
        if started is not None and started not in thread_ids:
            thread_ids.append(started)
    return tuple(events), tuple(thread_ids), first_thread_id, first_session_id


def _require_exact_directory(directory: Path, names: frozenset[str]) -> None:
    try:
        status = directory.lstat()
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise WorkflowStateError("action artifact directory is not exact")
        entries = tuple(directory.iterdir())
    except WorkflowStateError:
        raise
    except OSError as exc:
        raise WorkflowStateError("action artifact directory is unavailable") from exc
    if {entry.name for entry in entries} != names:
        raise WorkflowStateError("action artifact set is missing, additional, or replaced")
    for entry in entries:
        sha256_regular_file(entry)


def _model_from_json(
    path: Path,
    model: type[ModelT],
    label: str,
) -> ModelT:
    try:
        raw = _read_exact_bytes(path)
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        if not isinstance(value, dict):
            raise ValueError
        return model.model_validate(value)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, ValidationError) as exc:
        raise WorkflowStateError(f"{label} artifact is missing, malformed, or invalid") from exc


def _read_exact_bytes(path: Path) -> bytes:
    sha256_regular_file(path)
    try:
        return path.read_bytes()
    except OSError as exc:
        raise WorkflowStateError("durable artifact could not be read") from exc


def _structured_result_path(pending: PendingAction) -> Path:
    stage1 = Path(pending.artifact_path)
    run_directory = stage1.parents[2]
    destination = "worker" if pending.kind == "worker" else "audits"
    return run_directory / destination / f"{pending.action_id}.structured.json"


def _verify_time_agreement(
    started: str,
    ended: str,
    duration: float,
    label: str,
) -> None:
    start = _parse_timestamp(started, label)
    end = _parse_timestamp(ended, label)
    wall_duration = (end - start).total_seconds()
    if wall_duration < 0 or not math.isfinite(duration):
        raise WorkflowStateError(f"{label} timestamp order is invalid")
    tolerance = max(2.0, duration * 0.05)
    if abs(wall_duration - duration) > tolerance:
        raise WorkflowStateError(f"{label} duration contradicts its timestamps")


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowStateError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise WorkflowStateError(f"{label} timestamp must be UTC")
    return parsed


def _valid_thread_id(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", value) is not None


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")
