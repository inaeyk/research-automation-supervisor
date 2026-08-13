"""Process-isolated command surface for allowlisted qualified campaign operations."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import TextIO

from research_automation_supervisor.errors import SupervisorError
from research_automation_supervisor.prelaunch_authority import (
    CampaignLaunchRequestV1,
    freeze_launch_intent,
    load_launch_summary,
)
from research_automation_supervisor.qualified_campaign import (
    apply_qualified_human_response,
    export_qualified_campaign_bundle,
    qualified_campaign_repository,
    qualified_campaign_status,
    read_qualified_safe_artifact,
    resume_qualified_campaign,
    run_qualified_authentication,
    start_qualified_campaign,
    start_qualified_launch,
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
            "freeze",
            "launch-summary",
        ),
    )
    parser.add_argument("--authority", type=Path)
    parser.add_argument("--exchange", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--launch-token")
    parser.add_argument("--launch-authority-root", type=Path)
    parser.add_argument("--request-json")
    parser.add_argument("--expected-campaign")
    parser.add_argument("--expected-intent")
    parser.add_argument("--request")
    parser.add_argument("--token")
    parser.add_argument("--destination", type=Path)
    try:
        args = parser.parse_args(argv)
        if args.operation == "freeze":
            if args.launch_authority_root is None or args.request_json is None:
                parser.error("freeze requires launch authority and request")
            request = CampaignLaunchRequestV1.model_validate_json(args.request_json)
            reference = freeze_launch_intent(request, args.launch_authority_root)
            _print_json(reference.model_dump(mode="json"))
            return 0
        if args.operation == "launch-summary":
            if (
                args.launch_authority_root is None
                or args.launch_token is None
                or args.expected_campaign is None
                or args.expected_intent is None
            ):
                parser.error("launch-summary requires exact launch binding")
            summary = load_launch_summary(
                args.launch_authority_root,
                args.launch_token,
                expected_campaign_public_id=args.expected_campaign,
                expected_intent_sha256=args.expected_intent,
            )
            _print_json(summary.model_dump(mode="json"))
            return 0
        if args.operation == "authenticate":
            run_qualified_authentication()
            _print_json({"authenticated": True})
            return 0
        if args.authority is None or args.exchange is None:
            parser.error("campaign operations require --authority and --exchange")
        common = {"authority_directory": args.authority, "exchange_root": args.exchange}
        if args.operation == "start":
            if args.launch_token is not None and args.launch_authority_root is not None:
                result = start_qualified_launch(
                    args.launch_token,
                    args.launch_authority_root,
                    **common,
                )
            elif args.bundle is not None:
                # Retained only for the existing qualified-core regression surface.
                result = start_qualified_campaign(args.bundle, **common)
            else:
                parser.error("start requires core launch authority")
            _print_json(result.model_dump(mode="json"))
        elif args.operation == "status":
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
        else:
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


def _print_json(value: object, *, stream: TextIO = sys.stdout) -> None:
    print(json.dumps(value, ensure_ascii=True, sort_keys=True), file=stream)


if __name__ == "__main__":
    raise SystemExit(main())
