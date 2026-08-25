#!/usr/bin/python3 -I
"""Checkout entrypoint for unprivileged protected-release preparation only."""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    repository = Path(__file__).resolve(strict=True).parents[1]
    sys.path.insert(0, str(repository / "src"))
    from research_automation_supervisor.release_preparation import main as prepare_main

    return prepare_main()


if __name__ == "__main__":
    raise SystemExit(main())
