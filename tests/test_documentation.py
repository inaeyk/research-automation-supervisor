from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _public_markdown() -> list[Path]:
    return sorted(
        [ROOT / "README.md", ROOT / "CHANGELOG.md", *ROOT.glob("README_STAGE_*.md")]
        + list((ROOT / "docs").rglob("*.md"))
    )


def test_public_markdown_relative_links_exist() -> None:
    missing: list[str] = []
    for document in _public_markdown():
        for raw in MARKDOWN_LINK.findall(document.read_text(encoding="utf-8")):
            target = raw.strip("<>").split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                missing.append(f"{document.relative_to(ROOT)} -> {raw}")
    assert missing == []


def test_readme_has_public_release_sections_and_no_machine_username() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    required = (
        "# Research Automation Supervisor",
        "## What it solves",
        "## Architecture",
        "## Current status",
        "## Requirements",
        "## Installation",
        "## Ten-minute synthetic quick start",
        "## Define a project and substage",
        "## Worker and Auditor roles",
        "## Run one deterministic substage",
        "## Run a multi-task campaign",
        "## Read evidence and candidate exports",
        "## Resume, repair, and human pauses",
        "## Historical replay and evaluation",
        "## Security and permissions",
        "## Troubleshooting",
        "## Development",
        "## Repository layout",
        "## Known limitations and experimental components",
        "## License and contributions",
    )
    assert all(heading in readme for heading in required)
    combined = "\n".join(path.read_text(encoding="utf-8") for path in _public_markdown())
    machine_path = re.compile(
        r"/(?:home|Users)/[^/<\s]+/(?:researchrepo|Downloads)(?:/|\b)"
    )
    assert machine_path.search(combined) is None


def test_safe_validation_record_matches_locked_totals() -> None:
    path = ROOT / "docs/validation/five_task_historical_replay.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["campaign_id"] == "gl-five-visible-campaign-v1"
    assert record["run_token"] == "3283da26577c167ed3f93dd5be0aae09"
    assert record["candidate_manifest_sha256"] == (
        "7f891080fc205341dba3b9b0e0e56f24c9fb55d0e8a9b8173baef2056d6b7405"
    )
    assert record["functional_total"] == {"passed": 5, "total": 5}
    assert record["exact_match_total"] == {"passed": 0, "total": 5}
    assert len(record["tasks"]) == 5
    assert all(task["functional_passed"] is True for task in record["tasks"])
    assert all(task["exact_historical_identity"] is False for task in record["tasks"])


def test_evaluator_docs_keep_experimental_and_authoritative_paths_distinct() -> None:
    evaluation = (ROOT / "docs/evaluation.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for document in (evaluation, readme):
        assert "run-direct-historical-replay" in document
        assert "5/5" in document
        assert "0/5" in document
        assert "experimental" in document
    assert "evaluator_infrastructure_failure" in evaluation
    assert "0/5 and 4/5" in evaluation
    assert "superseded" in evaluation
