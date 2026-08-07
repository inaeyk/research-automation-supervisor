from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml  # type: ignore[import-untyped]

from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.errors import (
    PhysicsBenchmarkCampaignInputError,
    PhysicsBenchmarkCampaignStateError,
)
from research_automation_supervisor.physics_benchmark_blindness import (
    BlindBenchmarkLaunchAuthority,
    PhysicsBlindFixtureCatalogV1,
)
from research_automation_supervisor.physics_benchmark_campaign import (
    CampaignChildRequest,
    PhysicsBenchmarkCampaignServices,
    create_physics_benchmark_campaign,
    physics_benchmark_campaign_status,
    resume_physics_benchmark_campaign,
)
from research_automation_supervisor.physics_benchmark_campaign_models import (
    CampaignChildAuthorityV1,
    CampaignScoringArtifactsV1,
    CampaignTerminalChildV1,
)
from research_automation_supervisor.physics_benchmark_child_workflow import (
    blind_benchmark_physics_services,
)
from research_automation_supervisor.physics_benchmark_scoring import (
    ExactBenchmarkAggregateV1,
    ExactBenchmarkExpectedRunsV1,
    ExactBenchmarkObservedRun,
    ExactBenchmarkRunIdentityV1,
    ExactBenchmarkScoreReportV1,
    ExactCriterionAggregateV1,
    ExactRunSemanticScoreV1,
    PA2ProofIdentityV1,
)
from research_automation_supervisor.physics_workflow import PhysicsWorkflowServices
from research_automation_supervisor.workflow_recovery import (
    RECEIPT_ROOT,
    RecoveryExecutionV1,
)
from research_automation_supervisor.workflow_recovery_models import (
    RecoveryAttemptReceiptV1,
    RecoveryOutcomeV1,
    RecoveryPlanV1,
    RunIndexEntryV1,
    RunIndexV1,
)
from tests.test_physics_workflow import _physics_tree

ROOT = Path(__file__).parents[1]
CATALOG_PATH = ROOT / "examples/physics_auditor/benchmark_v1/scorer_only/catalog.json"
SHA = "a" * 64
NOW = "2026-08-07T00:00:00Z"


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _requests(tmp_path: Path, count: int = 2) -> tuple[CampaignChildRequest, ...]:
    requests: list[CampaignChildRequest] = []
    coordinates = (
        ("case_001", "variant_001", 1),
        ("case_001", "variant_002", 1),
        ("case_002", "variant_001", 1),
    )
    for index, (case_id, variant_id, repetition_id) in enumerate(coordinates[:count]):
        spec, _, _ = _physics_tree(tmp_path / f"fixture-{index}")
        value = yaml.safe_load(spec.read_text(encoding="utf-8"))
        value["substage_id"] = case_id
        spec.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
        requests.append(
            CampaignChildRequest(
                case_id=case_id,
                variant_id=variant_id,  # type: ignore[arg-type]
                repetition_id=repetition_id,
                specification_path=spec,
            )
        )
    return tuple(requests)


def _identity(
    child: CampaignChildAuthorityV1,
    catalog: PhysicsBlindFixtureCatalogV1,
    *,
    repetition_id: int | None = None,
) -> ExactBenchmarkRunIdentityV1:
    suffix = child.child_run_id
    return ExactBenchmarkRunIdentityV1(
        case_id=child.case_id,
        pair_id=catalog.pair(child.case_id).pair_id,
        variant_id=child.variant_id,
        repetition_id=child.repetition_id if repetition_id is None else repetition_id,
        catalog_id=catalog.catalog_id,
        catalog_sha256=catalog.canonical_sha256(),
        visible_manifest_sha256=_sha(f"visible-{suffix}"),
        scorer_authority_sha256=_sha(f"scorer-{suffix}"),
        scorer_root_manifest_sha256=_sha(f"root-{suffix}"),
        review_receipt_sha256=_sha(f"review-{suffix}"),
        contract_sha256=_sha(f"contract-{suffix}"),
        source_workspace_identity_sha256=_sha(f"workspace-{suffix}"),
        projection_manifest_sha256=_sha(f"projection-{suffix}"),
        pa2_proof_identities=(
            PA2ProofIdentityV1(
                completion_proof_id=f"proof-{suffix}",
                oracle_id="oracle",
                result_sha256=_sha(f"oracle-result-{suffix}"),
                completion_proof_sha256=_sha(f"oracle-proof-{suffix}"),
                trusted_intent_sha256=_sha(f"intent-{suffix}"),
                execution_policy_sha256=_sha(f"policy-{suffix}"),
            ),
        ),
        pa3_action_id=f"action-{suffix}",
        pa3_action_proof_sha256=_sha(f"pa3-proof-{suffix}"),
        pa3_launch_manifest_sha256=_sha(f"launch-{suffix}"),
        pa5c1_blindness_certificate_sha256=_sha(f"blind-{suffix}"),
        auditor_report_sha256=None,
        deterministic_route=None,
        finding_category_set=(),
        finding_severities=(),
        evidence_references=(),
        semantic_observations_sha256=_sha(f"semantic-{suffix}"),
        action_status="infrastructure_failure",
        failure_reason="scripted_fixture",
    )


def _report(expected: ExactBenchmarkExpectedRunsV1) -> ExactBenchmarkScoreReportV1:
    scores = tuple(
        ExactRunSemanticScoreV1(
            run_identity_sha256=item.canonical_sha256(),
            case_id=item.case_id,
            variant_id=item.variant_id,
            repetition_id=item.repetition_id,
            defect_category_recognition="not_applicable",
            severity_correctness="not_applicable",
            route_correctness="not_applicable",
            required_categories="not_applicable",
            acceptable_alternatives="not_applicable",
            forbidden_categories="not_applicable",
            forbidden_routes="not_applicable",
            evidence_validity="not_applicable",
            clean_case_pass="not_applicable",
            malformed_report=False,
            infrastructure_failure=True,
        )
        for item in expected.run_identities
    )
    empty = ExactCriterionAggregateV1(eligible_runs=0, correct_runs=0, rate=None)
    return ExactBenchmarkScoreReportV1(
        expected_run_manifest_sha256=expected.manifest_sha256,
        catalog_sha256=expected.catalog_sha256,
        run_scores=scores,
        aggregate=ExactBenchmarkAggregateV1(
            run_count=len(scores),
            defect_category_recognition=empty,
            severity_correctness=empty,
            route_correctness=empty,
            required_categories=empty,
            acceptable_alternatives=empty,
            forbidden_categories=empty,
            forbidden_routes=empty,
            evidence_validity=empty,
            clean_case_pass=empty,
            malformed_report_count=0,
            infrastructure_failure_count=len(scores),
        ),
    )


@dataclass
class FakeGateway:
    launch_result: Literal[
        "terminal",
        "active",
        "human",
        "evidence",
        "ambiguous",
        "active_process",
        "stale_process",
        "foreign_process",
        "failed",
        "missing",
    ] = "terminal"
    crash_inside_recovery: bool = False
    extra_child: bool = False
    wrong_entry_identity: bool = False
    wrong_repetition: bool = False
    proof_mismatch: bool = False

    def __post_init__(self) -> None:
        self.states: dict[str, str] = {}
        self.children: dict[str, CampaignChildAuthorityV1] = {}
        self.launch_count = 0
        self.recovery_count = 0
        self.scorer_count = 0

    def launch(
        self,
        child: CampaignChildAuthorityV1,
        catalog: PhysicsBlindFixtureCatalogV1,
        **kwargs: object,
    ) -> None:
        del catalog, kwargs
        self.launch_count += 1
        self.children[child.workflow_run_directory] = child
        if self.launch_result == "missing":
            return
        path = Path(child.workflow_run_directory)
        path.mkdir()
        (path / "fixture").write_text("scripted child\n", encoding="utf-8")
        self.states[child.workflow_run_directory] = self.launch_result
        if self.extra_child:
            (path.parent / "extra-unregistered-child").mkdir()

    def discover(self, runs_directory: Path, *, persist_cache: bool = False) -> RunIndexV1:
        del persist_cache
        entries: list[RunIndexEntryV1] = []
        for directory, state in sorted(self.states.items()):
            child = self.children[directory]
            terminal = state in {"terminal", "failed"}
            entry = RunIndexEntryV1(
                run_directory=directory,
                workflow_schema_version=2,
                substage_id=("case_999" if self.wrong_entry_identity else child.substage_id),
                run_token=child.run_token,
                status="failed"
                if state == "failed"
                else ("completed" if terminal else "physics_auditor_running"),
                completion="terminal" if terminal else "incomplete",
                journal_sequence=1,
                journal_hash=_sha(f"journal-head-{directory}-{state}"),
                updated_at=NOW,
                state_sha256=_sha(f"state-{directory}-{state}"),
                journal_sha256=_sha(f"journal-file-{directory}-{state}"),
            )
            entries.append(entry)
        source = {
            "runs_directory": str(runs_directory.resolve()),
            "entries": [item.model_dump(mode="json") for item in entries],
            "issues": [],
        }
        return RunIndexV1(
            runs_directory=str(runs_directory.resolve()),
            generated_at=NOW,
            entries=tuple(entries),
            issues=(),
            source_sha256=hashlib.sha256(canonical_json(source)).hexdigest(),
        )

    def plan(self, run_directory: Path) -> RecoveryPlanV1:
        child = self.children[str(run_directory)]
        entry = next(
            item
            for item in self.discover(run_directory.parent).entries
            if item.run_directory == str(run_directory)
        )
        state = self.states[str(run_directory)]
        common: dict[str, object] = {
            "run_directory": str(run_directory),
            "workflow_schema_version": 2,
            "substage_id": child.substage_id,
            "run_token": child.run_token,
            "observed_status": entry.status,
            "journal_sequence": entry.journal_sequence,
            "journal_hash": entry.journal_hash,
            "state_sha256": entry.state_sha256,
            "journal_sha256": entry.journal_sha256,
            "policy_sha256": _sha(f"policy-{run_directory}"),
            "workspace_reconciliation": "verified",
            "process_reconciliation": "not_applicable",
            "process_observations": (),
            "proof_reconciliation": "finalized_valid" if state == "terminal" else "before_launch",
            "pending_action_id": None,
            "pending_action_kind": None,
            "worker_session_id": None,
            "snapshots_synchronized": True,
        }
        if state == "terminal" or state == "failed":
            return RecoveryPlanV1(
                **common,
                disposition="already_terminal",
                operation="none",
                auto_resume_safe=False,
                reason_code="terminal_state_verified",
                next_step="No recovery action is needed.",
            )
        if state == "active":
            return RecoveryPlanV1(
                **common,
                disposition="auto_resume",
                operation="resume_workflow",
                auto_resume_safe=True,
                reason_code="safe_before_launch",
                next_step="Delegate exact recovery.",
            )
        if state in {"human", "evidence"}:
            return RecoveryPlanV1(
                **{
                    **common,
                    "observed_status": (
                        "human_review_paused" if state == "human" else "evidence_paused"
                    ),
                },
                disposition="reopen_pause",
                operation="reopen_pause",
                auto_resume_safe=False,
                reason_code="human_or_evidence_pause_reopened",
                next_step="Use the child review path.",
            )
        process = {
            "active_process": "active_matching",
            "stale_process": "stale_identity",
            "foreign_process": "foreign_host",
        }.get(state, "not_applicable")
        return RecoveryPlanV1(
            **{
                **common,
                "workspace_reconciliation": "invalid",
                "process_reconciliation": process,
                "proof_reconciliation": "missing" if state == "ambiguous" else "before_launch",
            },
            disposition="blocked",
            operation="none",
            auto_resume_safe=False,
            reason_code=(
                "ambiguous_post_launch_state" if state == "ambiguous" else f"{process}_process"
            ),
            next_step="Stop without reproducing child recovery.",
        )

    def recover(
        self,
        child: CampaignChildAuthorityV1,
        catalog: PhysicsBlindFixtureCatalogV1,
        plan: RecoveryPlanV1,
        *,
        repository_root: Path,
        attempt_token: str,
        recovery_services: object,
    ) -> RecoveryExecutionV1:
        del catalog, repository_root, recovery_services
        self.recovery_count += 1
        self.states[child.workflow_run_directory] = "terminal"
        attempt_id = f"recovery-{attempt_token}"
        receipt_dir = Path(child.workflow_run_directory).parent / RECEIPT_ROOT / "fake-run"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        plan_path = receipt_dir / f"{attempt_id}.plan.json"
        outcome_path = receipt_dir / f"{attempt_id}.outcome.json"
        receipt = RecoveryAttemptReceiptV1(
            attempt_id=attempt_id,
            created_at=NOW,
            plan_sha256=plan.canonical_sha256(),
            plan=plan,
        )
        outcome = RecoveryOutcomeV1(
            attempt_id=attempt_id,
            plan_sha256=plan.canonical_sha256(),
            status="resumed",
            run_directory=plan.run_directory,
            result_status="completed",
            reason_code="safe_recovery_completed",
            next_step="No recovery action is needed.",
            started_at=NOW,
            finished_at=NOW,
            plan_receipt_path=str(plan_path),
        )
        plan_path.write_text(
            json.dumps(receipt.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
        outcome_path.write_text(
            json.dumps(outcome.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
        if self.crash_inside_recovery:
            self.crash_inside_recovery = False
            raise KeyboardInterrupt
        return RecoveryExecutionV1(plan, outcome, plan_path, outcome_path)

    def bind(
        self,
        child: CampaignChildAuthorityV1,
        entry: RunIndexEntryV1,
        catalog: PhysicsBlindFixtureCatalogV1,
        *,
        repository_root: Path,
    ) -> CampaignTerminalChildV1:
        del repository_root
        if self.proof_mismatch:
            raise PhysicsBenchmarkCampaignStateError("scripted proof mismatch")
        identity = _identity(
            child,
            catalog,
            repetition_id=child.repetition_id + 1 if self.wrong_repetition else None,
        )
        return CampaignTerminalChildV1(
            child_run_id=child.child_run_id,
            child_authority_sha256=child.canonical_sha256(),
            workflow_status=entry.status,
            state_sha256=entry.state_sha256,
            journal_sha256=entry.journal_sha256,
            journal_hash=entry.journal_hash,
            scoring_identity=identity,
            scoring_artifacts=CampaignScoringArtifactsV1(
                contract_path=child.physics_contract_path,
                execution_config_path=child.auditor_config_path,
                workspace=child.workspace,
                oracle_evidence_root=str(Path(child.workflow_run_directory) / "oracles"),
                output_directory=str(Path(child.workflow_run_directory) / "audit"),
                attempt_number=1,
            ),
        )

    def score(
        self,
        expected: ExactBenchmarkExpectedRunsV1,
        observed: tuple[ExactBenchmarkObservedRun, ...],
        **kwargs: object,
    ) -> ExactBenchmarkScoreReportV1:
        del kwargs
        self.scorer_count += 1
        assert {item.identity.canonical_sha256() for item in observed} == {
            item.canonical_sha256() for item in expected.run_identities
        }
        return _report(expected)

    def services(self, checkpoint: Any = lambda _name: None) -> PhysicsBenchmarkCampaignServices:
        return PhysicsBenchmarkCampaignServices(
            child_launcher=self.launch,
            child_recovery_executor=self.recover,
            run_discoverer=self.discover,
            recovery_planner=self.plan,
            terminal_binder=self.bind,
            scorer=self.score,
            checkpoint=checkpoint,
        )


class CrashAt:
    def __init__(self, fragment: str) -> None:
        self.fragment = fragment
        self.crashed = False

    def __call__(self, name: str) -> None:
        if self.fragment in name and not self.crashed:
            self.crashed = True
            raise RuntimeError(f"injected campaign crash at {name}")


def _run(
    tmp_path: Path,
    gateway: FakeGateway,
    *,
    count: int = 2,
    checkpoint: Any = lambda _name: None,
) -> tuple[Path, Any]:
    run_directory = tmp_path / "campaign"
    state = create_physics_benchmark_campaign(
        "qualification-campaign",
        _requests(tmp_path / "inputs", count),
        run_directory=run_directory,
        catalog_path=CATALOG_PATH,
        repository_root=ROOT,
        services=gateway.services(checkpoint),
    )
    return run_directory, state


def test_complete_campaign_has_exact_bijection_and_idempotent_finalization(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    run_directory, state = _run(tmp_path, gateway)

    assert state.status == "completed"
    assert len(state.terminal_children) == 2
    assert gateway.launch_count == 2
    assert gateway.scorer_count == 1
    manifest = json.loads((run_directory / "campaign-manifest-v1.json").read_text())
    assert len(manifest["children"]) == 2
    assert {
        (item["case_id"], item["variant_id"], item["repetition_id"])
        for item in manifest["children"]
    } == {("case_001", "variant_001", 1), ("case_001", "variant_002", 1)}

    before = {
        path.relative_to(run_directory): path.read_bytes()
        for path in run_directory.rglob("*")
        if path.is_file() and path.name != "campaign.lock"
    }
    repeated = resume_physics_benchmark_campaign(run_directory, services=gateway.services())
    after = {
        path.relative_to(run_directory): path.read_bytes()
        for path in run_directory.rglob("*")
        if path.is_file() and path.name != "campaign.lock"
    }
    assert repeated == state
    assert before == after
    assert gateway.launch_count == 2
    assert gateway.scorer_count == 1
    assert physics_benchmark_campaign_status(run_directory, services=gateway.services()) == state


@pytest.mark.parametrize(
    "boundary",
    [
        "before_child_registration",
        "after_registration_before_child_launch",
        "between_child_runs",
        "after_child_terminal_before_campaign_observation",
        "after_all_children_terminal_before_scoring",
        "aggregation:before_pa5c2_scoring",
        "aggregation:after_aggregate_durable_write_before_completion",
        "finalization:after_completion_receipt_before_state",
    ],
)
def test_campaign_crash_boundaries_resume_without_duplicate_actions(
    tmp_path: Path, boundary: str
) -> None:
    gateway = FakeGateway()
    crash = CrashAt(boundary)
    run_directory = tmp_path / "campaign"
    with pytest.raises(RuntimeError, match="injected campaign crash"):
        create_physics_benchmark_campaign(
            "crash-campaign",
            _requests(tmp_path / "inputs"),
            run_directory=run_directory,
            catalog_path=CATALOG_PATH,
            repository_root=ROOT,
            services=gateway.services(crash),
        )

    state = resume_physics_benchmark_campaign(run_directory, services=gateway.services())
    assert state.status == "completed"
    assert gateway.launch_count == 2
    assert gateway.scorer_count == 1
    assert len(list((run_directory / "actions/launches").glob("*.json"))) == 2
    assert len(list((run_directory / "terminal-observations").glob("*.json"))) == 2


def test_interrupted_child_recovery_is_observed_not_reimplemented(tmp_path: Path) -> None:
    gateway = FakeGateway(launch_result="active", crash_inside_recovery=True)
    run_directory = tmp_path / "campaign"
    with pytest.raises(KeyboardInterrupt):
        create_physics_benchmark_campaign(
            "delegation-campaign",
            _requests(tmp_path / "inputs", 1),
            run_directory=run_directory,
            catalog_path=CATALOG_PATH,
            repository_root=ROOT,
            services=gateway.services(),
        )

    state = resume_physics_benchmark_campaign(run_directory, services=gateway.services())
    assert state.status == "completed"
    assert gateway.launch_count == 1
    assert gateway.recovery_count == 1
    actions = list((run_directory / "actions/recoveries").rglob("*.json"))
    assert any("pa5a_execute_recovery_plan" in path.read_text() for path in actions)


@pytest.mark.parametrize(
    ("child_state", "campaign_status"),
    [
        ("human", "human_review_required"),
        ("evidence", "insufficient_evidence"),
        ("ambiguous", "infrastructure_blocked"),
        ("active_process", "running"),
        ("stale_process", "infrastructure_blocked"),
        ("foreign_process", "infrastructure_blocked"),
        ("failed", "child_failed"),
    ],
)
def test_campaign_preserves_pa5a_and_child_state_routing(
    tmp_path: Path, child_state: str, campaign_status: str
) -> None:
    gateway = FakeGateway(launch_result=child_state)  # type: ignore[arg-type]
    _, state = _run(tmp_path, gateway, count=1)
    assert state.status == campaign_status
    assert gateway.recovery_count == 0
    assert gateway.scorer_count == 0


@pytest.mark.parametrize(
    "mutation",
    ["missing", "extra", "stale_authority", "wrong_repetition", "proof_mismatch"],
)
def test_adversarial_child_sets_and_proofs_fail_closed(tmp_path: Path, mutation: str) -> None:
    gateway = FakeGateway(
        launch_result="missing" if mutation == "missing" else "terminal",
        extra_child=mutation == "extra",
        wrong_entry_identity=mutation == "stale_authority",
        wrong_repetition=mutation == "wrong_repetition",
        proof_mismatch=mutation == "proof_mismatch",
    )
    _, state = _run(tmp_path, gateway, count=1)
    assert state.status == "infrastructure_blocked"
    assert gateway.scorer_count == 0


def test_duplicate_expected_child_is_rejected_before_launch(tmp_path: Path) -> None:
    gateway = FakeGateway()
    request = _requests(tmp_path / "inputs", 1)[0]
    with pytest.raises(PhysicsBenchmarkCampaignInputError):
        create_physics_benchmark_campaign(
            "duplicate-campaign",
            (request, request),
            run_directory=tmp_path / "campaign",
            catalog_path=CATALOG_PATH,
            repository_root=ROOT,
            services=gateway.services(),
        )
    assert gateway.launch_count == 0


def test_terminal_child_substitution_after_completion_is_rejected(tmp_path: Path) -> None:
    gateway = FakeGateway()
    run_directory, _ = _run(tmp_path, gateway, count=1)
    child_path = next(iter(gateway.states))
    gateway.states[child_path] = "failed"
    with pytest.raises(PhysicsBenchmarkCampaignStateError):
        resume_physics_benchmark_campaign(run_directory, services=gateway.services())


def test_child_adapter_supplies_pa5c1_authority_only_through_pa4_seam(
    tmp_path: Path,
) -> None:
    requests = _requests(tmp_path, 1)
    gateway = FakeGateway(launch_result="missing")
    run_directory, state = _run(tmp_path / "manifest", gateway, count=1)
    del requests, state
    manifest = json.loads((run_directory / "campaign-manifest-v1.json").read_text())
    child = CampaignChildAuthorityV1.model_validate(manifest["children"][0])
    catalog = PhysicsBlindFixtureCatalogV1.model_validate(
        json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    )
    calls: list[tuple[str, object]] = []

    def runner(**kwargs: object) -> object:
        calls.append(("run", kwargs["blindness_authority"]))
        return object()

    def resumer(**kwargs: object) -> object:
        calls.append(("resume", kwargs["blindness_authority"]))
        return object()

    base = PhysicsWorkflowServices(
        auditor_runner=runner,
        auditor_resumer=resumer,
    )
    services = blind_benchmark_physics_services(
        child,
        catalog,
        repository_root=ROOT,
        base=base,
    )
    services.auditor_runner(example=True)
    services.auditor_resumer(example=True)
    assert [item[0] for item in calls] == ["run", "resume"]
    assert all(isinstance(item[1], BlindBenchmarkLaunchAuthority) for item in calls)
    assert all(
        item[1].variant_id == child.variant_id
        for item in calls
        if isinstance(item[1], BlindBenchmarkLaunchAuthority)
    )


def test_campaign_layer_has_no_direct_pa2_pa3_or_model_invocation() -> None:
    campaign_source = (
        ROOT / "src/research_automation_supervisor/physics_benchmark_campaign.py"
    ).read_text(encoding="utf-8")
    child_source = (
        ROOT / "src/research_automation_supervisor/physics_benchmark_child_workflow.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "run_physics_oracle",
        "resume_physics_oracle",
        "run_physics_auditor",
        "resume_physics_auditor",
        "run_prepared_codex",
        "CodexRunRequest",
    ):
        assert forbidden not in campaign_source
        assert forbidden not in child_source
    assert "run_substage(" in child_source
    assert "execute_recovery_plan(" in child_source
