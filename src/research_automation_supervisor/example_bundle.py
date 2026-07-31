"""Materialize the bundled non-model synthetic quick-start project."""

from __future__ import annotations

import os
import shutil
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path


class ExampleBundleError(RuntimeError):
    """Raised when the synthetic example cannot be materialized safely."""


def materialize_synthetic_example(output: Path) -> Path:
    """Copy the installed synthetic workflow into one new directory."""
    try:
        destination = output.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExampleBundleError("example output path could not be resolved") from exc
    if output.exists() or output.is_symlink():
        raise ExampleBundleError("example output must not already exist")
    try:
        parent = destination.parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ExampleBundleError("example output parent must already exist") from exc
    if parent.is_symlink() or not parent.is_dir() or destination.parent != parent:
        raise ExampleBundleError("example output parent must be an exact directory")
    source = resources.files("research_automation_supervisor").joinpath(
        "example_data",
        "synthetic_quickstart",
    )
    staging = parent / f".{destination.name}.staging"
    if staging.exists() or staging.is_symlink():
        raise ExampleBundleError("stale example staging path already exists")
    try:
        staging.mkdir(mode=0o700)
        _copy_resource_tree(source, staging)
        codex = staging / "project/tools/codex"
        codex.chmod(0o755)
        os.replace(staging, destination)
    except Exception as exc:
        if staging.is_dir() and not staging.is_symlink():
            shutil.rmtree(staging)
        if isinstance(exc, ExampleBundleError):
            raise
        raise ExampleBundleError("synthetic example could not be materialized") from exc
    return destination


def _copy_resource_tree(source: Traversable, destination: Path) -> None:
    if not source.is_dir():
        raise ExampleBundleError("installed synthetic example data is missing")
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        target = destination / child.name
        if child.is_dir():
            target.mkdir(mode=0o700)
            _copy_resource_tree(child, target)
        elif child.is_file():
            target.write_bytes(child.read_bytes())
            target.chmod(0o600)
        else:
            raise ExampleBundleError("installed example contains an unsupported object")
