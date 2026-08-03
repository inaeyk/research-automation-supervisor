from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from research_automation_supervisor.errors import (
    PhysicsAuditorInputError,
    PhysicsAuditorIntegrityError,
    PhysicsOracleInputError,
)
from research_automation_supervisor.physics_auditor_evidence import (
    collect_changed_path_manifest,
    discover_physics_auditor_evidence,
)
from research_automation_supervisor.physics_auditor_models import (
    PhysicsAuditorChangedPathManifestV1,
)
from research_automation_supervisor.physics_auditor_projection import (
    AUTHORITY_DIRECTORY,
    PhysicsAuditorProjectionPlan,
    build_physics_auditor_projection,
    materialize_physics_auditor_projection,
    verify_physics_auditor_projection,
)
from research_automation_supervisor.physics_models import load_physics_task_contract
from research_automation_supervisor.physics_oracle_workspace import (
    collect_physics_oracle_workspace_identity,
)
from tests.test_physics_auditor_execution import SYNTHETIC, _workspace


def _plan(workspace: Path, tmp_path: Path) -> PhysicsAuditorProjectionPlan:
    contract = load_physics_task_contract(SYNTHETIC / "contract.yaml")
    identity = collect_physics_oracle_workspace_identity(workspace)
    changed = collect_changed_path_manifest(workspace, identity)
    evidence = tmp_path / "empty-evidence"
    evidence.mkdir(exist_ok=True)
    discovered = discover_physics_auditor_evidence(
        contract=contract,
        task_id="synthetic-task",
        workspace=workspace,
        workspace_identity=identity,
        changed_paths=changed,
        oracle_evidence_root=evidence,
    )
    return build_physics_auditor_projection(
        contract=contract,
        evidence_index=discovered.index,
        changed_paths=changed,
        source_workspace=workspace,
        oracle_program_paths=discovered.oracle_program_paths,
    )


def test_projection_manifest_is_deterministic_exact_and_excludes_repository_authority(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    (workspace / ".gitignore").write_text("ignored-secret\nprotected/\n", encoding="ascii")
    (workspace / "ignored-secret").write_text("not audit authority\n", encoding="ascii")
    protected = workspace / "protected"
    protected.mkdir()
    (protected / "historical_gold.json").write_text("{}\n", encoding="ascii")
    subprocess.run(("/usr/bin/git", "-C", workspace, "add", ".gitignore"), check=True)
    subprocess.run(
        ("/usr/bin/git", "-C", workspace, "commit", "-qm", "ignore private material"),
        check=True,
    )

    first = _plan(workspace, tmp_path)
    second = _plan(workspace, tmp_path)
    assert first.manifest == second.manifest
    assert first.manifest.canonical_sha256() == second.manifest.canonical_sha256()

    projection = tmp_path / "projection"
    materialize_physics_auditor_projection(first, projection)
    verify_physics_auditor_projection(first.manifest, projection)
    visible = {path.relative_to(projection).as_posix() for path in projection.rglob("*")}
    assert visible == {item.path for item in first.manifest.objects}
    assert "implementation.py" in visible
    assert "derivation.md" in visible
    assert "oracle.py" not in visible
    assert ".git" not in visible
    assert "ignored-secret" not in visible
    assert not any(path.startswith("protected") for path in visible)
    assert f"{AUTHORITY_DIRECTORY}/evidence-index.json" in visible


def test_projection_rejects_symlink_and_path_escape(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    source = workspace / "implementation.py"
    source.unlink()
    source.symlink_to("derivation.md")

    with pytest.raises(PhysicsAuditorInputError, match="symlinks"):
        _plan(workspace, tmp_path)


def test_projection_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "note.md").write_text("outside\n", encoding="ascii")
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    identity = collect_physics_oracle_workspace_identity(workspace)
    changed = PhysicsAuditorChangedPathManifestV1(
        workspace_identity_sha256=identity.canonical_sha256(),
        paths=("linked/note.md",),
    )
    contract = load_physics_task_contract(SYNTHETIC / "contract.yaml")
    evidence = tmp_path / "empty-evidence"
    evidence.mkdir()

    with pytest.raises(PhysicsAuditorInputError):
        discovered = discover_physics_auditor_evidence(
            contract=contract,
            task_id="synthetic-task",
            workspace=workspace,
            workspace_identity=identity,
            changed_paths=changed,
            oracle_evidence_root=evidence,
        )
        build_physics_auditor_projection(
            contract=contract,
            evidence_index=discovered.index,
            changed_paths=changed,
            source_workspace=workspace,
            oracle_program_paths=(),
        )


def test_projection_rejects_ignored_declared_input(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    (workspace / ".gitignore").write_text("derivation.md\n", encoding="ascii")
    subprocess.run(
        ("/usr/bin/git", "-C", workspace, "rm", "--cached", "derivation.md"),
        check=True,
        stdout=subprocess.DEVNULL,
    )

    with pytest.raises(PhysicsAuditorInputError, match="ignored"):
        _plan(workspace, tmp_path)


def test_projection_rejects_nested_repository_input(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    nested = workspace / "nested"
    nested.mkdir()
    shutil.copyfile(workspace / "derivation.md", nested / "note.md")
    subprocess.run(("/usr/bin/git", "-C", nested, "init", "-q"), check=True)
    with pytest.raises(PhysicsOracleInputError, match="unsafe path"):
        collect_physics_oracle_workspace_identity(workspace)


def test_projection_rejects_unsupported_special_object(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    identity = collect_physics_oracle_workspace_identity(workspace)
    fifo = workspace / "diagnostic.pipe"
    os.mkfifo(fifo)
    changed = PhysicsAuditorChangedPathManifestV1(
        workspace_identity_sha256=identity.canonical_sha256(),
        paths=("diagnostic.pipe",),
    )
    contract = load_physics_task_contract(SYNTHETIC / "contract.yaml")
    evidence = tmp_path / "empty-evidence"
    evidence.mkdir()

    with pytest.raises(PhysicsAuditorInputError, match="unsupported special object"):
        discover_physics_auditor_evidence(
            contract=contract,
            task_id="synthetic-task",
            workspace=workspace,
            workspace_identity=identity,
            changed_paths=changed,
            oracle_evidence_root=evidence,
        )


def test_projection_substitution_is_detected(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    plan = _plan(workspace, tmp_path)
    projection = tmp_path / "projection"
    materialize_physics_auditor_projection(plan, projection)
    target = projection / "implementation.py"
    target.chmod(0o600)
    target.write_text("substituted\n", encoding="ascii")

    with pytest.raises(PhysicsAuditorIntegrityError, match="projected"):
        verify_physics_auditor_projection(plan.manifest, projection)
