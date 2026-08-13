#!/usr/bin/env python3
"""Mechanically verify every pre-PA-5C4 scientific/review file is byte-unchanged."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

BASE_COMMIT = "0ee2bc91c067eab6efd9e3e115fad63c0f811a45"
PROTECTED_PREFIXES = (
    "src/research_automation_supervisor/",
    "tests/",
    "examples/physics_auditor/",
    "docs/physics_",
    "docs/workflow_recovery.md",
    "PA5C1_",
)


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    tracked = _git(repository, "ls-tree", "-r", "--name-only", BASE_COMMIT).decode(
        "utf-8"
    )
    paths = tuple(
        sorted(
            path
            for path in tracked.splitlines()
            if path.startswith(PROTECTED_PREFIXES)
        )
    )
    changed: list[str] = []
    digest = hashlib.sha256()
    for relative in paths:
        expected = _git(repository, "show", f"{BASE_COMMIT}:{relative}")
        path = repository / relative
        try:
            observed = path.read_bytes()
        except OSError:
            changed.append(relative)
            continue
        if observed != expected:
            changed.append(relative)
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(observed).digest())
    report = {
        "schema_version": 1,
        "base_commit": BASE_COMMIT,
        "protected_file_count": len(paths),
        "protected_tree_sha256": digest.hexdigest(),
        "unchanged": not changed,
        "changed_paths": changed,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not changed else 1


def _git(repository: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
    )
    return completed.stdout


if __name__ == "__main__":
    raise SystemExit(main())
