#!/usr/bin/env python3
"""Prepare the stable user-owned CODEX_HOME before the managed venv exists."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from research_automation_supervisor.custodian_errors import (  # noqa: E402
    CustodianEnvironmentError,
)
from research_automation_supervisor.managed_codex import (  # noqa: E402
    initialize_managed_codex_home,
    prepare_managed_codex_home,
    verified_managed_codex_home,
)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments not in (["initialize"], ["verify"]) and not (
        len(arguments) == 2 and arguments[0] == "acceptance-test"
    ):
        print("one explicit initialize or verify operation is required", file=sys.stderr)
        return 2
    try:
        if arguments[0] == "initialize":
            managed_home = initialize_managed_codex_home()
        elif arguments[0] == "verify":
            managed_home = verified_managed_codex_home()
        else:
            managed_home = prepare_managed_codex_home(Path(arguments[1]))
    except CustodianEnvironmentError as exc:
        print(str(exc), file=sys.stderr)
        return 7
    print(managed_home)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
