from __future__ import annotations

import hashlib
from pathlib import Path

from research_automation_supervisor.git_evidence import (
    collect_git_evidence,
    record_git_baseline,
)
from tests.workflow_helpers import create_workflow_tree, git


def test_clean_baseline_and_tracked_untracked_deleted_evidence(tmp_path: Path) -> None:
    _, project, _ = create_workflow_tree(tmp_path)
    baseline = record_git_baseline(project)
    assert baseline.clean
    (project / "src" / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    git(project, "add", "src/tracked.txt")
    git(project, "commit", "-q", "-m", "tracked")
    baseline = record_git_baseline(project)
    (project / "src" / "tracked.txt").unlink()
    (project / "src" / "new.txt").write_text("new content\n", encoding="utf-8")

    evidence = collect_git_evidence(
        project,
        baseline,
        ("src/**",),
        ("control/**",),
        tmp_path / "evidence",
    )

    kinds = {entry.path: entry.kind for entry in evidence.changed_paths}
    assert kinds == {"src/new.txt": "untracked", "src/tracked.txt": "deleted"}
    assert evidence.scope_compliant
    assert evidence.patch_complete
    assert Path(evidence.patch_artifact).is_file()


def test_protected_wins_and_symlink_escape_is_detected(tmp_path: Path) -> None:
    _, project, _ = create_workflow_tree(tmp_path)
    baseline = record_git_baseline(project)
    (project / "control" / "contract.md").write_text("changed\n", encoding="utf-8")
    (project / "src" / "escape").symlink_to("../../../outside")

    evidence = collect_git_evidence(
        project,
        baseline,
        ("**",),
        ("control/**",),
        tmp_path / "evidence",
    )

    reasons = {(finding.path, finding.reason) for finding in evidence.scope_findings}
    assert ("control/contract.md", "protected_path") in reasons
    assert ("src/escape", "symlink_escape") in reasons
    assert not evidence.scope_compliant


def test_oversized_patch_stores_hash_and_explicit_truncation_marker(tmp_path: Path) -> None:
    _, project, _ = create_workflow_tree(tmp_path)
    baseline = record_git_baseline(project)
    (project / "src" / "large.txt").write_text("x" * 4096, encoding="utf-8")

    evidence = collect_git_evidence(
        project,
        baseline,
        ("src/**",),
        ("control/**",),
        tmp_path / "evidence",
        max_patch_bytes=128,
    )

    stored = Path(evidence.patch_artifact).read_bytes()
    assert not evidence.patch_complete
    assert b"AUDIT MUST NOT RUN" in stored
    assert evidence.patch_sha256 != hashlib.sha256(stored).hexdigest()


def test_rename_and_type_change_are_reported_without_index_mutation(tmp_path: Path) -> None:
    _, project, _ = create_workflow_tree(tmp_path)
    (project / "src/old.txt").write_text("rename\n", encoding="utf-8")
    (project / "src/type.txt").write_text("regular\n", encoding="utf-8")
    git(project, "add", ".")
    git(project, "commit", "-q", "-m", "tracked paths")
    baseline = record_git_baseline(project)
    git(project, "mv", "src/old.txt", "src/new-name.txt")
    (project / "src/type.txt").unlink()
    (project / "src/type.txt").symlink_to("new-name.txt")

    evidence = collect_git_evidence(
        project,
        baseline,
        ("src/**",),
        ("control/**",),
        tmp_path / "evidence",
    )

    assert any(entry.kind == "renamed" for entry in evidence.changed_paths)
    assert any(entry.kind == "type_changed" for entry in evidence.changed_paths)
    assert evidence.index_tree_sha256_before == evidence.index_tree_sha256_after
