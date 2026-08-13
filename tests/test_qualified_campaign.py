from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from research_automation_supervisor.custodian_models import (
    CampaignInputBundleV1,
    FrozenInputFileV1,
    RepositoryAuthorityV1,
    render_qualified_acceptance_runner,
)
from research_automation_supervisor.qualified_campaign import (
    BUNDLE_FILE,
    QualifiedCampaignLocatorV1,
    _materialize_visible_authority,
    qualified_campaign_status,
)
from research_automation_supervisor.replay_campaign_sources import (
    load_replay_campaign_specification,
)
from tests.custodian_helpers import create_repository, git


def qualified_bundle(tmp_path: Path) -> CampaignInputBundleV1:
    repository = create_repository(tmp_path)
    campaign_root = tmp_path / "workspace" / "campaign-cccccccccccc"
    campaign_root.mkdir(parents=True)
    worktree = campaign_root / "repository"
    git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    support = worktree / ".research-supervisor"
    support.mkdir()
    (support / "acceptance.py").write_bytes(render_qualified_acceptance_runner(sys.executable))
    git(worktree, "add", ".research-supervisor/acceptance.py")
    git(worktree, "commit", "-q", "-m", "add qualified runner")
    authority = RepositoryAuthorityV1(
        source_kind="existing_folder",
        source_display="source-repository",
        source_locator_sha256="1" * 64,
        prepared_workspace=str(worktree),
        baseline_commit=git(worktree, "rev-parse", "HEAD"),
        baseline_tree=git(worktree, "rev-parse", "HEAD^{tree}"),
        repository_id="source-repository",
    )
    return CampaignInputBundleV1.freeze(
        campaign_public_id="campaign-cccccccccccc",
        human_name="Qualified campaign",
        repository=authority,
        research_contract=FrozenInputFileV1.from_bytes(
            "contract.md", b"Do the bounded research task.\n"
        ),
        research_plan=FrozenInputFileV1.from_bytes(
            "plan.md", b"Implement and independently audit the task.\n"
        ),
        initial_task=FrozenInputFileV1.from_bytes("task.md", b"Implement the research task.\n"),
    )


def test_qualified_materialization_compiles_plain_inputs_into_existing_strict_core(
    tmp_path: Path,
) -> None:
    bundle = qualified_bundle(tmp_path)
    authority = tmp_path / "qualified"
    authority.mkdir()
    manifest = _materialize_visible_authority(bundle, authority)
    prepared = load_replay_campaign_specification(manifest)
    assert prepared.specification.campaign_id == bundle.campaign_public_id
    assert prepared.tasks[0].stage2.contract.content == bundle.research_contract.content_bytes()
    assert prepared.tasks[0].stage2.specification.allowed_paths == ("**",)
    assert prepared.tasks[0].stage2.acceptance_tests[0].specification.argv == (
        "/usr/bin/python3",
        ".research-supervisor/acceptance.py",
        "repository_integrity",
    )
    assert not prepared.tasks[0].stage2.specification.checkpoint_after
    control = manifest.parent / ".research-supervisor-control"
    assert manifest.stat().st_mode & 0o222 == 0
    assert all(path.stat().st_mode & 0o222 == 0 for path in control.rglob("*") if path.is_file())


def test_operator_status_cannot_fabricate_completed_from_unverified_state(tmp_path: Path) -> None:
    bundle = qualified_bundle(tmp_path)
    authority = tmp_path / "qualified"
    authority.mkdir()
    (authority / BUNDLE_FILE).write_text(
        json.dumps(bundle.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    run = authority / "runs" / f"{bundle.campaign_public_id}-{bundle.bundle_sha256[:32]}"
    run.mkdir(parents=True)
    locator = QualifiedCampaignLocatorV1(
        campaign_public_id=bundle.campaign_public_id,
        bundle_sha256=bundle.bundle_sha256,
        visible_manifest=str(tmp_path / "missing-manifest.json"),
        core_run_directory=str(run),
        prepared_workspace=bundle.repository.prepared_workspace,
    )
    (authority / "qualified-locator-v1.json").write_text(
        json.dumps(locator.model_dump(mode="json"), sort_keys=True),
        encoding="utf-8",
    )
    (run / "state.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    with pytest.raises(Exception, match="locator was substituted"):
        qualified_campaign_status(
            authority_directory=authority,
            exchange_root=tmp_path / "exchange",
        )


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="Bubblewrap unavailable")
def test_repository_acceptance_cannot_escape_workspace(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside-campaign-authority.txt"
    (repository / "test_escape.py").write_text(
        "from pathlib import Path\n"
        "def test_escape():\n"
        "    try:\n"
        "        (Path.cwd().parent / 'outside-campaign-authority.txt').write_text('mutated')\n"
        "    except OSError:\n"
        "        pass\n",
        encoding="utf-8",
    )
    runner = repository / "acceptance.py"
    runner.write_bytes(render_qualified_acceptance_runner(sys.executable))
    completed = subprocess.run(
        [sys.executable, str(runner), "python_pytest"],
        cwd=repository,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0
    assert not outside.exists()
