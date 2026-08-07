"""Qualified PA-4/PA-5A child adapter for PA-5C1 blind benchmark authority.

This module only composes existing qualified entrypoints.  It does not implement an
oracle, a model launch, a workflow state machine, or recovery semantics.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from research_automation_supervisor.physics_benchmark_blindness import (
    BlindBenchmarkLaunchAuthority,
    PhysicsBlindFixtureCatalogV1,
)
from research_automation_supervisor.physics_benchmark_campaign_models import (
    CampaignChildAuthorityV1,
)
from research_automation_supervisor.physics_workflow import (
    DEFAULT_PHYSICS_WORKFLOW_SERVICES,
    PhysicsWorkflowServices,
)
from research_automation_supervisor.workflow_engine import (
    DEFAULT_WORKFLOW_SERVICES,
    WorkflowServices,
    run_substage,
)
from research_automation_supervisor.workflow_recovery import (
    DEFAULT_RECOVERY_SERVICES,
    RecoveryExecutionV1,
    RecoveryServices,
    execute_recovery_plan,
)
from research_automation_supervisor.workflow_recovery_models import RecoveryPlanV1


def blind_benchmark_physics_services(
    child: CampaignChildAuthorityV1,
    catalog: PhysicsBlindFixtureCatalogV1,
    *,
    repository_root: Path,
    base: PhysicsWorkflowServices = DEFAULT_PHYSICS_WORKFLOW_SERVICES,
) -> PhysicsWorkflowServices:
    """Bind one child's PA-5C1 authority through the existing PA-4 service seam."""
    authority = BlindBenchmarkLaunchAuthority(
        catalog=catalog,
        pair=catalog.pair(child.case_id),
        variant_id=child.variant_id,
        repository_root=repository_root,
    )

    def run_blind_auditor(**kwargs: Any) -> Any:
        return base.auditor_runner(**kwargs, blindness_authority=authority)

    def resume_blind_auditor(**kwargs: Any) -> Any:
        return base.auditor_resumer(**kwargs, blindness_authority=authority)

    return replace(
        base,
        auditor_runner=run_blind_auditor,
        auditor_resumer=resume_blind_auditor,
    )


def launch_qualified_benchmark_child(
    child: CampaignChildAuthorityV1,
    catalog: PhysicsBlindFixtureCatalogV1,
    *,
    child_runs_directory: Path,
    repository_root: Path,
    workflow_services: WorkflowServices = DEFAULT_WORKFLOW_SERVICES,
    physics_services: PhysicsWorkflowServices = DEFAULT_PHYSICS_WORKFLOW_SERVICES,
) -> None:
    """Create one ordinary PA-4 run at its manifest-bound deterministic identity."""
    child_workflow_services = replace(
        workflow_services,
        token_factory=lambda: child.run_token,
    )
    child_physics_services = blind_benchmark_physics_services(
        child,
        catalog,
        repository_root=repository_root,
        base=physics_services,
    )
    run_substage(
        Path(child.specification_path),
        runs_dir=child_runs_directory,
        services=child_workflow_services,
        physics_services=child_physics_services,
    )


def execute_qualified_benchmark_child_recovery(
    child: CampaignChildAuthorityV1,
    catalog: PhysicsBlindFixtureCatalogV1,
    plan: RecoveryPlanV1,
    *,
    repository_root: Path,
    attempt_token: str,
    recovery_services: RecoveryServices = DEFAULT_RECOVERY_SERVICES,
) -> RecoveryExecutionV1:
    """Delegate the exact plan to PA-5A with the child's unchanged PA-5C1 authority."""
    physics_services = blind_benchmark_physics_services(
        child,
        catalog,
        repository_root=repository_root,
        base=recovery_services.physics_services,
    )
    services = replace(
        recovery_services,
        physics_services=physics_services,
        attempt_token=lambda: attempt_token,
    )
    return execute_recovery_plan(plan, services=services)
