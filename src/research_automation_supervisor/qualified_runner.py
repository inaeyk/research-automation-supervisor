"""Process-isolated command surface for allowlisted qualified campaign operations."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import TextIO

from research_automation_supervisor.core_authority_client import (
    DEFAULT_CORE_SOCKET,
    UnixCoreAuthorityClient,
)
from research_automation_supervisor.errors import SupervisorError
from research_automation_supervisor.managed_codex import (
    verified_managed_codex_home,
    verify_managed_codex_installation,
)
from research_automation_supervisor.qualified_campaign import (
    apply_qualified_human_response,
    export_qualified_campaign_bundle,
    qualified_campaign_repository,
    qualified_campaign_status,
    read_qualified_safe_artifact,
    resume_qualified_campaign,
    run_qualified_authentication,
    start_qualified_launch,
    verify_qualified_campaign_binding,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "operation",
        choices=(
            "start",
            "status",
            "resume",
            "respond",
            "artifact",
            "export",
            "repository",
            "authenticate",
        ),
    )
    parser.add_argument("--authority", type=Path)
    parser.add_argument("--exchange", type=Path)
    parser.add_argument("--launch-intent")
    parser.add_argument("--core-socket", type=Path, default=DEFAULT_CORE_SOCKET)
    parser.add_argument("--expected-campaign")
    parser.add_argument("--request")
    parser.add_argument("--token")
    parser.add_argument("--destination", type=Path)
    try:
        args = parser.parse_args(argv)
        _seal_production_git_environment()
        _establish_shared_workspace_umask()
        if args.operation == "authenticate":
            run_qualified_authentication()
            _print_json({"authenticated": True})
            return 0
        if args.authority is None or args.exchange is None:
            parser.error("campaign operations require --authority and --exchange")
        if args.launch_intent is None or args.expected_campaign is None:
            parser.error("campaign operations require one exact core launch intent")
        core = UnixCoreAuthorityClient(args.core_socket)
        common = {"authority_directory": args.authority, "exchange_root": args.exchange}
        if args.operation == "start":
            result = start_qualified_launch(
                args.launch_intent,
                core,
                expected_campaign_public_id=args.expected_campaign,
                **common,
            )
            _print_json(result.model_dump(mode="json"))
        else:
            summary = core.verify_start_intent(
                args.launch_intent,
                expected_campaign_public_id=args.expected_campaign,
            )
            verify_qualified_campaign_binding(
                args.authority,
                expected_campaign_public_id=args.expected_campaign,
                expected_bundle_sha256=summary.input_bundle_sha256,
            )
        if args.operation == "status":
            result = qualified_campaign_status(**common)
            _print_json(result.model_dump(mode="json"))
        elif args.operation == "resume":
            result = resume_qualified_campaign(**common)
            _print_json(result.model_dump(mode="json"))
        elif args.operation == "respond":
            if args.request is None:
                parser.error("respond requires --request")
            result = apply_qualified_human_response(request_sha256=args.request, **common)
            _print_json(result.model_dump(mode="json"))
        elif args.operation == "artifact":
            if args.token is None:
                parser.error("artifact requires --token")
            media_type, content = read_qualified_safe_artifact(token=args.token, **common)
            _print_json(
                {
                    "media_type": media_type,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                }
            )
        elif args.operation == "export":
            if args.destination is None:
                parser.error("export requires --destination")
            exported = export_qualified_campaign_bundle(destination=args.destination, **common)
            _print_json({"path": str(exported)})
        elif args.operation == "repository":
            repository_path = qualified_campaign_repository(**common)
            _print_json({"path": str(repository_path)})
    except SupervisorError as exc:
        _print_json(
            {
                "error": type(exc).__name__,
                "message": str(exc)[:2048],
            },
            stream=sys.stderr,
        )
        return 4
    except SystemExit:
        raise
    except Exception:
        _print_json(
            {
                "error": "qualified_internal_error",
                "message": (
                    "The qualified operation stopped safely. Technical details were logged locally."
                ),
            },
            stream=sys.stderr,
        )
        return 4
    return 0


def _establish_shared_workspace_umask() -> None:
    """Keep later Worker-created workspace content cooperative with Core.

    The root installer provisions the mutable workspace root with the shared GID
    and SGID inheritance.  The ordinary qualified runner supplies the matching
    creation mask so its workflow children retain group write permission without
    a later chmod/chown or any elevated capability.
    """
    os.umask(0o007)


def _seal_production_git_environment() -> None:
    """Ignore host config; snapshot-local config is trusted-generated authority."""
    verify_managed_codex_installation(require_code_mode_host=True)
    codex_home = verified_managed_codex_home()
    os.environ.clear()
    os.environ.update(
        {
            "CODEX_HOME": str(codex_home),
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "XDG_CONFIG_HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "LANG": "C.UTF-8",
        }
    )
    # The only command-scope setting is ownership qualification for the
    # cross-UID workspace.  It does not neutralize repository configuration.
    os.environ["GIT_CONFIG_COUNT"] = "1"
    os.environ["GIT_CONFIG_KEY_0"] = "safe.directory"
    os.environ["GIT_CONFIG_VALUE_0"] = "*"


def _print_json(value: object, *, stream: TextIO = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=True, sort_keys=True), file=stream)


if __name__ == "__main__":
    raise SystemExit(main())
