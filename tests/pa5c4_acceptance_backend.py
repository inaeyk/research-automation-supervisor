#!/usr/bin/env python3
"""Qualification-only backend selected by the real Windows launcher."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from pa5c4_acceptance_services import (
    DeterministicCampaignRunner,
    DeterministicEnvironment,
)

from research_automation_supervisor.custodian import CampaignCustodian
from research_automation_supervisor.custodian_server import serve


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--readiness-instance", required=True)
    parser.add_argument("--acceptance-scenario", required=True, type=Path)
    args = parser.parse_args()
    scenario = args.acceptance_scenario.resolve(strict=True)
    if scenario.name != "pa5c4-real-browser-scenario.json":
        raise SystemExit("invalid qualification scenario")
    logging.basicConfig(
        filename=args.data_dir / "custodian-state" / "technical-details.log",
        level=logging.INFO,
    )
    custodian = CampaignCustodian(
        args.data_dir,
        runner=DeterministicCampaignRunner(),
        environment_inspector=DeterministicEnvironment(scenario.parent),
    )
    serve(
        args.data_dir,
        host=args.host,
        port=args.port,
        readiness_instance=args.readiness_instance,
        custodian=custodian,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
