"""Safe evidence indexing and verified PA-2 consumption for PA-3."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from pydantic import ValidationError

from research_automation_supervisor.durable_state import render_json_bytes
from research_automation_supervisor.errors import (
    PhysicsAuditorInputError,
    PhysicsAuditorIntegrityError,
    PhysicsOracleError,
)
from research_automation_supervisor.physics_auditor_models import (
    PhysicsAuditorChangedPathManifestV1,
    PhysicsAuditorDeclaredEvidenceV1,
    PhysicsAuditorEvidenceIndexV1,
    PhysicsAuditorOracleEvidenceV1,
    PhysicsAuditorOracleProofBindingV1,
    PhysicsAuditorWorkspaceFileV1,
)
from research_automation_supervisor.physics_models import PhysicsTaskContractV1
from research_automation_supervisor.physics_oracle_execution import (
    PROOF_FILE,
    RESULT_FILE,
    verify_physics_oracle_completion,
)
from research_automation_supervisor.physics_oracle_models import (
    PhysicsOracleCompletionProofV1,
    PhysicsOracleExecutionResultV1,
    PhysicsOracleWorkspaceIdentityV1,
)

_GIT = Path("/usr/bin/git")
_GIT_ENVIRONMENT = {
    "GIT_ASKPASS": "/nonexistent",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_PROTOCOL_FROM_USER": "0",
    "GIT_SSH_COMMAND": "/nonexistent",
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": "/nonexistent",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
    "SSH_ASKPASS": "/nonexistent",
    "XDG_CONFIG_HOME": "/nonexistent",
}
_PROTECTED_COMPONENTS = frozenset(
    {"gold", "hidden", "historical_gold", "protected", "private_evaluation"}
)


@dataclass(frozen=True)
class DiscoveredPhysicsAuditorEvidence:
    """Strictly parsed but not yet independently verified PA-2 evidence."""

    index: PhysicsAuditorEvidenceIndexV1
    bindings: tuple[PhysicsAuditorOracleProofBindingV1, ...]
    oracle_directories: tuple[tuple[str, Path], ...]


def collect_changed_path_manifest(
    workspace: Path,
    identity: PhysicsOracleWorkspaceIdentityV1,
) -> PhysicsAuditorChangedPathManifestV1:
    """Collect a deterministic candidate-delta path list without patch contents."""
    changed = set(_nul_git_paths(workspace, ("diff", "--name-only", "-z", "HEAD", "--")))
    changed.update(
        _nul_git_paths(
            workspace,
            ("ls-files", "--others", "--exclude-standard", "-z"),
        )
    )
    for path in changed:
        _validate_public_relative_path(path)
    return PhysicsAuditorChangedPathManifestV1(
        workspace_identity_sha256=identity.canonical_sha256(),
        paths=tuple(sorted(changed)),
    )


def discover_physics_auditor_evidence(
    *,
    contract: PhysicsTaskContractV1,
    task_id: str,
    workspace: Path,
    workspace_identity: PhysicsOracleWorkspaceIdentityV1,
    changed_paths: PhysicsAuditorChangedPathManifestV1,
    oracle_evidence_root: Path,
) -> DiscoveredPhysicsAuditorEvidence:
    """Build the safe index from strict proof/result files without trusting them yet."""
    root = _canonical_directory(oracle_evidence_root, "oracle evidence root")
    _validate_workspace_visibility(workspace)
    candidate_directories = _oracle_action_directories(root)
    contract_oracles = {item.id: item for item in contract.oracles}
    results: dict[
        str, tuple[Path, PhysicsOracleExecutionResultV1, PhysicsOracleCompletionProofV1]
    ] = {}
    proof_ids: set[str] = set()
    for directory in candidate_directories:
        result = _load_exact_model(
            directory / RESULT_FILE,
            PhysicsOracleExecutionResultV1,
            "PA-2 oracle result",
        )
        proof = _load_exact_model(
            directory / PROOF_FILE,
            PhysicsOracleCompletionProofV1,
            "PA-2 completion proof",
        )
        oracle_id = result.request.oracle_id
        if oracle_id not in contract_oracles:
            raise PhysicsAuditorIntegrityError(
                "oracle evidence references an oracle outside the task contract"
            )
        if oracle_id in results or result.request.action_id in proof_ids:
            raise PhysicsAuditorIntegrityError("oracle evidence IDs must be unique")
        if result.request.task_id != task_id:
            raise PhysicsAuditorIntegrityError("oracle evidence task ID does not match")
        results[oracle_id] = (directory, result, proof)
        proof_ids.add(result.request.action_id)

    oracle_entries: list[PhysicsAuditorOracleEvidenceV1] = []
    bindings: list[PhysicsAuditorOracleProofBindingV1] = []
    directories: list[tuple[str, Path]] = []
    workspace_hash = workspace_identity.canonical_sha256()
    for oracle in contract.oracles:
        discovered = results.get(oracle.id)
        if discovered is None:
            oracle_entries.append(
                PhysicsAuditorOracleEvidenceV1(
                    oracle_id=oracle.id,
                    required=oracle.required,
                    availability="missing",
                    completion_proof_id=None,
                    result_sha256=None,
                    completion_proof_sha256=None,
                    trusted_intent_sha256=None,
                    execution_policy_sha256=None,
                    workspace_identity_sha256=None,
                    status=None,
                    failure_reason=None,
                    declared_outcome=None,
                    structured_result_sha256=None,
                )
            )
            continue
        directory, result, proof = discovered
        result_hash = result.canonical_sha256()
        proof_hash = proof.canonical_sha256()
        oracle_entries.append(
            PhysicsAuditorOracleEvidenceV1(
                oracle_id=oracle.id,
                required=oracle.required,
                availability="verified",
                completion_proof_id=result.request.action_id,
                result_sha256=result_hash,
                completion_proof_sha256=proof_hash,
                trusted_intent_sha256=result.request.trusted_intent_sha256,
                execution_policy_sha256=result.request.execution_policy_sha256,
                workspace_identity_sha256=workspace_hash,
                status=result.status,
                failure_reason=result.failure_reason,
                declared_outcome=result.declared_outcome,
                structured_result_sha256=result.structured_result_sha256,
                artifacts=result.artifacts,
            )
        )
        bindings.append(
            PhysicsAuditorOracleProofBindingV1(
                completion_proof_id=result.request.action_id,
                oracle_id=oracle.id,
                result_sha256=result_hash,
                completion_proof_sha256=proof_hash,
                trusted_intent_sha256=result.request.trusted_intent_sha256,
                execution_policy_sha256=result.request.execution_policy_sha256,
            )
        )
        directories.append((oracle.id, directory))

    declared = tuple(
        PhysicsAuditorDeclaredEvidenceV1(
            id=item.id,
            kind=item.kind,
            path=item.path,
            required_for=item.required_for,
            availability=(
                "declared"
                if item.path is None
                else "present"
                if (workspace / item.path).exists()
                else "missing"
            ),
        )
        for item in contract.evidence
    )
    declared_paths: dict[str, set[str]] = {}
    for item in contract.evidence:
        if item.path is not None:
            _validate_public_relative_path(item.path)
            declared_paths.setdefault(item.path, set()).add(item.id)
    for oracle in contract.oracles:
        if oracle.kind in {"artifact", "derivation", "document"}:
            _validate_public_relative_path(oracle.reference)
            declared_paths.setdefault(oracle.reference, set()).add(oracle.id)
    all_paths = set(declared_paths) | set(changed_paths.paths)
    files = tuple(
        _workspace_file(
            workspace,
            path,
            tuple(sorted(declared_paths.get(path, set()))),
            path in changed_paths.paths,
        )
        for path in sorted(all_paths)
    )
    index = PhysicsAuditorEvidenceIndexV1(
        schema_version=1,
        contract_sha256=contract.canonical_sha256(),
        workspace_identity_sha256=workspace_hash,
        changed_path_manifest_sha256=changed_paths.canonical_sha256(),
        convention_ids=tuple(item.id for item in contract.conventions),
        assumption_ids=tuple(item.id for item in contract.assumptions),
        required_identity_ids=tuple(item.id for item in contract.required_identities),
        limiting_case_ids=tuple(item.id for item in contract.limiting_cases),
        forbidden_claim_ids=tuple(item.id for item in contract.forbidden_claims),
        declared_evidence=declared,
        workspace_files=files,
        oracle_evidence=tuple(oracle_entries),
    )
    return DiscoveredPhysicsAuditorEvidence(
        index=index,
        bindings=tuple(sorted(bindings, key=lambda item: item.oracle_id)),
        oracle_directories=tuple(sorted(directories)),
    )


def verify_discovered_physics_auditor_evidence(
    *,
    discovered: DiscoveredPhysicsAuditorEvidence,
    contract: PhysicsTaskContractV1,
    task_id: str,
    workspace_identity: PhysicsOracleWorkspaceIdentityV1,
) -> None:
    """Independently close every discovered PA-2 proof before model launch."""
    expected = {item.oracle_id: item for item in discovered.index.oracle_evidence}
    contract_ids = {item.id for item in contract.oracles}
    if set(expected) != contract_ids:
        raise PhysicsAuditorIntegrityError("evidence index does not cover declared oracles")
    for oracle_id, directory in discovered.oracle_directories:
        try:
            result = verify_physics_oracle_completion(directory)
        except PhysicsOracleError as exc:
            raise PhysicsAuditorIntegrityError(
                "PA-2 oracle completion proof verification failed"
            ) from exc
        entry = expected[oracle_id]
        if (
            entry.availability != "verified"
            or result.request.task_id != task_id
            or result.request.contract_sha256 != contract.canonical_sha256()
            or result.request.oracle_id != oracle_id
            or result.integrity_verdict != "unchanged"
            or result.initial_workspace_identity != workspace_identity
            or result.final_workspace_identity != workspace_identity
            or result.canonical_sha256() != entry.result_sha256
            or result.completion_proof_sha256 != entry.completion_proof_sha256
            or result.request.action_id != entry.completion_proof_id
            or result.request.trusted_intent_sha256 != entry.trusted_intent_sha256
            or result.request.execution_policy_sha256 != entry.execution_policy_sha256
        ):
            raise PhysicsAuditorIntegrityError(
                "PA-2 oracle evidence contradicts task, policy, or workspace authority"
            )
    for oracle in contract.oracles:
        entry = expected[oracle.id]
        if oracle.required and entry.availability not in {"verified", "missing"}:
            raise PhysicsAuditorIntegrityError(
                "required oracle evidence is neither verified nor explicitly missing"
            )


def validate_report_evidence_index(
    report: Any,
    index: PhysicsAuditorEvidenceIndexV1,
) -> None:
    """Reject model-invented IDs, paths, artifacts, and impossible line ranges."""
    declared = {item.id: item for item in index.declared_evidence}
    oracles = {item.oracle_id: item for item in index.oracle_evidence}
    files = {item.path: item for item in index.workspace_files}
    contract_locators = {
        "schema_version",
        "profile",
        "human_gate",
        "audit_policy",
        *(f"conventions.{item}" for item in index.convention_ids),
        *(f"assumptions.{item}" for item in index.assumption_ids),
        *(f"required_identities.{item}" for item in index.required_identity_ids),
        *(f"limiting_cases.{item}" for item in index.limiting_case_ids),
        *(f"evidence.{item.id}" for item in index.declared_evidence),
        *(f"oracles.{item}" for item in oracles),
        *(f"forbidden_claims.{item}" for item in index.forbidden_claim_ids),
    }
    references = []
    for check in report.checks:
        if check.target_kind == "oracle":
            oracle = oracles.get(check.target_id)
            if oracle is None:
                raise PhysicsAuditorIntegrityError("report invented an oracle check")
            if oracle.availability == "missing" and check.status != "unresolved":
                raise PhysicsAuditorIntegrityError(
                    "report treated explicitly missing oracle evidence as observed"
                )
            if (
                oracle.availability == "verified"
                and oracle.status != "passed"
                and check.status == "passed"
            ):
                raise PhysicsAuditorIntegrityError(
                    "report contradicted the verified PA-2 oracle outcome"
                )
        references.extend(check.evidence)
    for finding in report.findings:
        references.extend(finding.evidence)
    for question in report.unresolved_questions:
        references.extend(question.evidence)
    for reference in references:
        if reference.kind == "task_contract":
            if reference.reference not in contract_locators:
                raise PhysicsAuditorIntegrityError("report invented a contract reference")
        elif reference.kind == "oracle":
            if reference.reference not in oracles:
                raise PhysicsAuditorIntegrityError("report invented an oracle reference")
        elif reference.kind in {"test", "artifact", "numerical"}:
            declared_item = declared.get(cast(str, reference.reference))
            if declared_item is None or declared_item.kind != reference.kind:
                raise PhysicsAuditorIntegrityError("report invented a declared evidence ID")
        else:
            path = cast(str, reference.path)
            workspace_item = files.get(path)
            if workspace_item is None or workspace_item.kind == "missing":
                raise PhysicsAuditorIntegrityError(
                    "report invented or cited a missing workspace path"
                )
            if reference.line_end is not None and (
                workspace_item.line_count is None or reference.line_end > workspace_item.line_count
            ):
                raise PhysicsAuditorIntegrityError("report source line range exceeds the file")


def _workspace_file(
    workspace: Path,
    relative: str,
    evidence_ids: tuple[str, ...],
    changed: bool,
) -> PhysicsAuditorWorkspaceFileV1:
    _validate_public_relative_path(relative)
    path = workspace / relative
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise PhysicsAuditorInputError("declared workspace path could not be resolved") from exc
    if not resolved.is_relative_to(workspace):
        raise PhysicsAuditorInputError("declared workspace path escapes the workspace")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return PhysicsAuditorWorkspaceFileV1(
            path=relative,
            kind="missing",
            byte_length=0,
            mode=None,
            sha256=None,
            line_count=None,
            declared_evidence_ids=evidence_ids,
            changed=changed,
        )
    except OSError as exc:
        raise PhysicsAuditorInputError(
            "declared workspace evidence could not be inspected"
        ) from exc
    mode = stat.S_IMODE(metadata.st_mode)
    kind: Literal["regular", "symlink", "directory", "missing"]
    if stat.S_ISREG(metadata.st_mode):
        digest = hashlib.sha256()
        line_count = 0
        last = b""
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
                    line_count += chunk.count(b"\n")
                    last = chunk[-1:]
        except OSError as exc:
            raise PhysicsAuditorInputError("declared workspace file could not be read") from exc
        if metadata.st_size and last != b"\n":
            line_count += 1
        kind = "regular"
        sha = digest.hexdigest()
    elif stat.S_ISLNK(metadata.st_mode):
        try:
            target = os.readlink(path)
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PhysicsAuditorInputError("declared workspace symlink is invalid") from exc
        if not resolved.is_relative_to(workspace):
            raise PhysicsAuditorInputError("declared workspace symlink escapes the workspace")
        raw = os.fsencode(target)
        kind = "symlink"
        sha = hashlib.sha256(raw).hexdigest()
        line_count = None
    elif stat.S_ISDIR(metadata.st_mode):
        kind = "directory"
        sha = hashlib.sha256(b"").hexdigest()
        line_count = None
    else:
        raise PhysicsAuditorInputError("declared evidence uses an unsupported special object")
    return PhysicsAuditorWorkspaceFileV1(
        path=relative,
        kind=kind,
        byte_length=metadata.st_size,
        mode=mode,
        sha256=sha,
        line_count=line_count,
        declared_evidence_ids=evidence_ids,
        changed=changed,
    )


def _oracle_action_directories(root: Path) -> tuple[Path, ...]:
    if (root / RESULT_FILE).is_file() or (root / PROOF_FILE).is_file():
        if not (root / RESULT_FILE).is_file() or not (root / PROOF_FILE).is_file():
            raise PhysicsAuditorIntegrityError("PA-2 evidence root is incomplete")
        return (root,)
    directories: list[Path] = []
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise PhysicsAuditorInputError("oracle evidence root could not be enumerated") from exc
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_symlink() or not entry.is_dir():
            raise PhysicsAuditorInputError("oracle evidence root contains an unsafe entry")
        if not (entry / RESULT_FILE).is_file() or not (entry / PROOF_FILE).is_file():
            raise PhysicsAuditorIntegrityError(
                "oracle evidence child is not a completed PA-2 action"
            )
        directories.append(entry.resolve(strict=True))
    return tuple(directories)


def _canonical_directory(path: Path, label: str) -> Path:
    if ".." in path.parts:
        raise PhysicsAuditorInputError(f"{label} contains parent traversal")
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PhysicsAuditorInputError(f"{label} is unavailable") from exc
    if absolute != resolved or path.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise PhysicsAuditorInputError(f"{label} must be a canonical non-symlink directory")
    if label == "oracle evidence root" and any(
        part.casefold() in _PROTECTED_COMPONENTS for part in resolved.parts
    ):
        raise PhysicsAuditorInputError("protected historical evidence root is forbidden")
    return resolved


def _validate_public_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(part.casefold() in _PROTECTED_COMPONENTS for part in path.parts)
    ):
        raise PhysicsAuditorInputError("protected or unsafe evidence path is forbidden")


def _nul_git_paths(workspace: Path, arguments: tuple[str, ...]) -> tuple[str, ...]:
    try:
        completed = subprocess.run(
            (_GIT, "-C", workspace, *arguments),
            env=_GIT_ENVIRONMENT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PhysicsAuditorInputError("changed-path manifest could not be collected") from exc
    if completed.returncode != 0 or len(completed.stdout) > 256 * 1024 * 1024:
        raise PhysicsAuditorInputError("changed-path manifest Git query failed")
    raw_items = completed.stdout.split(b"\x00")
    if raw_items and raw_items[-1] == b"":
        raw_items.pop()
    try:
        return tuple(item.decode("utf-8") for item in raw_items)
    except UnicodeDecodeError as exc:
        raise PhysicsAuditorInputError("changed-path manifest contains a non-UTF-8 path") from exc


def _validate_workspace_visibility(workspace: Path) -> None:
    paths = _nul_git_paths(
        workspace,
        ("ls-files", "--cached", "--others", "--exclude-standard", "-z"),
    )
    if len(paths) > 100_000:
        raise PhysicsAuditorInputError("workspace visibility manifest exceeds its path bound")
    for relative in paths:
        _validate_public_relative_path(relative)
        path = workspace / relative
        try:
            metadata = path.lstat()
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise PhysicsAuditorInputError("workspace visibility path is unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode) and not resolved.is_relative_to(workspace):
            raise PhysicsAuditorInputError("workspace symlink exposes data outside the workspace")


def _load_exact_model(path: Path, model: type[Any], label: str) -> Any:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
        parsed = model.model_validate(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise PhysicsAuditorIntegrityError(f"{label} is malformed or unavailable") from exc
    if raw != render_json_bytes(parsed.model_dump(mode="json")):
        raise PhysicsAuditorIntegrityError(f"{label} is not canonically encoded")
    return parsed


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
