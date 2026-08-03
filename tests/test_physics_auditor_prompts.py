from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from research_automation_supervisor.physics_auditor_evidence import (
    collect_changed_path_manifest,
    discover_physics_auditor_evidence,
)
from research_automation_supervisor.physics_auditor_models import (
    PhysicsAuditorChangedPathManifestV1,
    PhysicsAuditorEvidenceIndexV1,
)
from research_automation_supervisor.physics_auditor_prompts import (
    PHYSICS_AUDITOR_PROMPT_TEMPLATE_SHA256,
    build_physics_auditor_prompt,
)
from research_automation_supervisor.physics_models import load_physics_task_contract
from research_automation_supervisor.physics_oracle_workspace import (
    collect_physics_oracle_workspace_identity,
)

ROOT = Path(__file__).parents[1]
SYNTHETIC = ROOT / "examples/physics_auditor/synthetic"


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name in ("implementation.py", "derivation.md", "oracle.py"):
        (workspace / name).write_bytes((SYNTHETIC / "clean" / name).read_bytes())
    subprocess.run(("/usr/bin/git", "-C", workspace, "init", "-q"), check=True)
    subprocess.run(
        ("/usr/bin/git", "-C", workspace, "config", "user.name", "Synthetic Test"),
        check=True,
    )
    subprocess.run(
        (
            "/usr/bin/git",
            "-C",
            workspace,
            "config",
            "user.email",
            "synthetic@example.invalid",
        ),
        check=True,
    )
    subprocess.run(("/usr/bin/git", "-C", workspace, "add", "."), check=True)
    git_environment = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    }
    subprocess.run(
        ("/usr/bin/git", "-C", workspace, "commit", "-qm", "synthetic baseline"),
        check=True,
        env=git_environment,
    )
    return workspace


def test_prompt_is_deterministic_ordered_and_has_golden_hash(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    contract = load_physics_task_contract(SYNTHETIC / "contract.yaml")
    identity = collect_physics_oracle_workspace_identity(workspace)
    changed = collect_changed_path_manifest(workspace, identity)
    discovered = discover_physics_auditor_evidence(
        contract=contract,
        task_id="synthetic-task",
        workspace=workspace,
        workspace_identity=identity,
        changed_paths=changed,
        oracle_evidence_root=evidence,
    )

    golden_changed = PhysicsAuditorChangedPathManifestV1(
        workspace_identity_sha256="a" * 64,
        paths=(),
    )
    golden_index = PhysicsAuditorEvidenceIndexV1.model_validate(
        {
            **discovered.index.model_dump(mode="json"),
            "workspace_identity_sha256": "a" * 64,
            "changed_path_manifest_sha256": golden_changed.canonical_sha256(),
        }
    )
    first = build_physics_auditor_prompt(contract, golden_index, golden_changed)
    second = build_physics_auditor_prompt(contract, golden_index, golden_changed)

    assert first == second
    assert first.template_sha256 == PHYSICS_AUDITOR_PROMPT_TEMPLATE_SHA256
    assert first.rendered_sha256 == hashlib.sha256(first.content).hexdigest()
    assert first.content.endswith(b"\n")
    text = first.content.decode("ascii")
    assert [text.index(f"{number}.") for number in range(1, 12)] == sorted(
        text.index(f"{number}.") for number in range(1, 12)
    )
    assert "/tmp/" not in text
    assert "chain-of-thought" not in text.casefold()
    golden = json.loads((SYNTHETIC / "prompt-golden.json").read_text())
    assert first.rendered_sha256 == golden["missing_evidence_prompt_sha256"]
    assert first.byte_count == golden["missing_evidence_prompt_byte_count"]
    assert first.template_sha256 == golden["prompt_template_sha256"]
    assert first.output_schema_sha256 == golden["output_schema_sha256"]
