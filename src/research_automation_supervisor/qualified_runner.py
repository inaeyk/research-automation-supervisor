"""Process-isolated command surface for allowlisted qualified campaign operations."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import TextIO

from research_automation_supervisor.errors import SupervisorError
from research_automation_supervisor.qualified_campaign import (
    apply_qualified_human_response,
    export_qualified_campaign_bundle,
    qualified_campaign_status,
    read_qualified_safe_artifact,
    resume_qualified_campaign,
    run_qualified_authentication,
    start_qualified_campaign,
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
            "authenticate",
        ),
    )
    parser.add_argument("--authority", type=Path)
    parser.add_argument("--exchange", type=Path)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--request")
    parser.add_argument("--token")
    parser.add_argument("--destination", type=Path)
    try:
        args = parser.parse_args(argv)
        if args.operation == "authenticate":
            run_qualified_authentication()
            _print_json({"authenticated": True})
            return 0
        if args.authority is None or args.exchange is None:
            parser.error("campaign operations require --authority and --exchange")
        common = {"authority_directory": args.authority, "exchange_root": args.exchange}
        if args.operation == "start":
            if args.bundle is None:
                parser.error("start requires --bundle")
            result = start_qualified_campaign(args.bundle, **common)
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
        else:
            if args.destination is None:
                parser.error("export requires --destination")
            exported = export_qualified_campaign_bundle(destination=args.destination, **common)
            _print_json({"path": str(exported)})
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
