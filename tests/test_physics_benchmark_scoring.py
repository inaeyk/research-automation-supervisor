from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.errors import PhysicsBenchmarkScoringIntegrityError
from research_automation_supervisor.physics_auditor_execution import (
    build_test_qualified_physics_auditor_codex,
    run_physics_auditor,
)
from research_automation_supervisor.physics_benchmark_blindness import (
    BlindBenchmarkLaunchAuthority,
    PhysicsBlindFixtureCatalogV1,
    load_blind_fixture_catalog,
)
from research_automation_supervisor.physics_benchmark_scoring import (
    ExactBenchmarkObservedRun,
    ExactBenchmarkRunArtifacts,
    ExactBenchmarkRunIdentityV1,
    bind_exact_benchmark_run,
    issue_expected_run_manifest,
    score_exact_physics_benchmark,
)
from research_automation_supervisor.physics_oracle_execution import run_physics_oracle

ROOT = Path(__file__).parents[1]
BENCHMARK = ROOT / "examples/physics_auditor/benchmark_v1"
CATALOG_PATH = BENCHMARK / "scorer_only/catalog.json"
BASE_CONFIG = ROOT / "examples/physics_auditor/synthetic/execution-config.yaml"
PYTHON = Path("/usr/bin/python3").resolve(strict=True)


@dataclass(frozen=True)
class BoundRun:
    identity: ExactBenchmarkRunIdentityV1
    artifacts: ExactBenchmarkRunArtifacts

    def observed(self) -> ExactBenchmarkObservedRun:
        return ExactBenchmarkObservedRun(identity=self.identity, artifacts=self.artifacts)


@dataclass(frozen=True)
class BoundSuite:
    catalog: PhysicsBlindFixtureCatalogV1
    defective_one: BoundRun
    defective_two: BoundRun
    clean: BoundRun
    malformed: BoundRun
    infrastructure: BoundRun


def _git_workspace(tmp_path: Path, catalog: PhysicsBlindFixtureCatalogV1, key: str) -> Path:
    case_id, variant_id = key.split(":", 1)
    pair = catalog.pair(case_id)
    variant = next(item for item in pair.variants if item.variant_id == variant_id)
    workspace = tmp_path / f"workspace-{case_id}-{variant_id}"
    shutil.copytree(ROOT / variant.visible_root, workspace)
    subprocess.run(("git", "init", "-q", str(workspace)), check=True)
    subprocess.run(("git", "-C", str(workspace), "config", "user.name", "Exact Scorer"), check=True)
    subprocess.run(
        ("git", "-C", str(workspace), "config", "user.email", "scorer@example.invalid"),
        check=True,
    )
    subprocess.run(("git", "-C", str(workspace), "add", "."), check=True)
    subprocess.run(
        ("git", "-C", str(workspace), "commit", "-qm", "fixture"),
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
        },
    )
    return workspace


def _oracle_catalog(workspace: Path, destination: Path) -> Path:
    program = workspace / "raw_measurement_oracle.py"
    payload = {
        "schema_version": 1,
        "catalog_id": "exact-scoring-oracles",
        "environment_profiles": [
            {"schema_version": 1, "id": "minimal-python", "profile": "minimal_python_v1"}
        ],
        "intents": [
            {
                "schema_version": 1,
                "id": "raw_measurement_oracle",
                "executable": {
                    "schema_version": 1,
                    "policy": "isolated_system_python_v1",
                    "path": str(PYTHON),
                    "sha256": hashlib.sha256(PYTHON.read_bytes()).hexdigest(),
                },
                "program": {
                    "path": "raw_measurement_oracle.py",
                    "sha256": hashlib.sha256(program.read_bytes()).hexdigest(),
                },
                "argv": [
                    str(PYTHON),
                    "-I",
                    "-S",
                    "-B",
                    "raw_measurement_oracle.py",
                    "observations.json",
                ],
                "execution_policy": {
                    "schema_version": 1,
                    "policy_id": "exact-scoring-raw-only",
                    "isolation_backend": "bubblewrap_unshare_all_v1",
                    "working_directory": "workspace_root",
                    "workspace_access": "read_only",
                    "scratch_output": "scratch_only",
                    "network": "disabled",
                    "environment_profile_id": "minimal-python",
                    "timeout_seconds": 30,
                    "max_stdout_bytes": 65536,
                    "max_stderr_bytes": 65536,
                    "accepted_exit_codes": [0],
                    "structured_output_schema": "none",
                    "required_artifacts": [],
                },
            }
        ],
    }
    destination.write_bytes(json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n")
    return destination


def _reference(kind: str, reference: str | None, path: str | None) -> dict[str, object]:
    return {
        "kind": kind,
        "reference": reference,
        "path": path,
        "line_start": 1 if path == "implementation.py" else None,
        "line_end": 2 if path == "implementation.py" else None,
    }


def _report(*, defective: bool) -> dict[str, object]:
    oracle = _reference("oracle", "raw_measurement_oracle", None)
    source = _reference("source", None, "implementation.py")
    numerical = _reference("numerical", "raw_measurements", None)
    document = _reference("document", None, "evidence.md")
    checks: list[dict[str, object]] = [
        {
            "id": "check_control",
            "target_kind": "limiting_case",
            "target_id": "control_limit",
            "status": "passed",
            "evidence_sufficiency": "sufficient",
            "evidence": [numerical, oracle],
            "rationale": "The declared zero-input control remains zero.",
        },
        {
            "id": "check_oracle",
            "target_kind": "oracle",
            "target_id": "raw_measurement_oracle",
            "status": "passed",
            "evidence_sufficiency": "sufficient",
            "evidence": [oracle],
            "rationale": "The verified raw measurement normalization completed.",
        },
        {
            "id": "check_primary",
            "target_kind": "required_identity",
            "target_id": "primary_identity",
            "status": "failed" if defective else "passed",
            "evidence_sufficiency": "sufficient",
            "evidence": [document, numerical, oracle, source],
            "rationale": (
                "The source slope differs from the declared identity."
                if defective
                else "The source and observations satisfy the declared identity."
            ),
        },
    ]
    findings: list[dict[str, object]] = []
    if defective:
        findings.append(
            {
                "id": "finding_primary",
                "severity": "high",
                "category": "violated_identity",
                "status": "open",
                "disposition": "repairable",
                "check_ids": ["check_primary"],
                "forbidden_claim_ids": [],
                "evidence": [oracle, source],
                "statement": "The implementation uses a slope of 1.5 instead of 2.",
                "required_action": "Restore the declared response slope.",
            }
        )
    return {
        "schema_version": 1,
        "profile": "physics_implementation",
        "verdict": "fail_repairable" if defective else "pass",
        "evidence_sufficiency": "sufficient",
        "summary": (
            "The declared response identity is violated."
            if defective
            else "The declared response identity and control are satisfied."
        ),
        "human_gate_triggers": [],
        "checks": checks,
        "findings": findings,
        "unresolved_questions": [],
    }


def _fake_codex(
    destination: Path,
    *,
    mode: Literal["defective", "clean", "malformed", "infrastructure"],
) -> Path:
    if mode == "defective":
        output = json.dumps(_report(defective=True), sort_keys=True, separators=(",", ":"))
        exit_code = 0
    elif mode == "clean":
        output = json.dumps(_report(defective=False), sort_keys=True, separators=(",", ":"))
        exit_code = 0
    elif mode == "malformed":
        output = "{}"
        exit_code = 0
    else:
        output = ""
        exit_code = 7
    destination.write_text(
        "#!/usr/bin/python3\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-cli exact-scoring-test-v1')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.buffer.read()\n"
        "index = sys.argv.index('--output-last-message')\n"
        f"Path(sys.argv[index + 1]).write_text({output!r}, encoding='utf-8')\n"
        "print(json.dumps({'type':'thread.started','thread_id':'fresh-exact-scoring'}))\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    destination.chmod(0o700)
    return destination


def _execution_config(destination: Path, executable: Path) -> Path:
    value = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))
    value["trusted_executable"] = {
        "path": str(executable),
        "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
    }
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return destination


def _run(
    root: Path,
    catalog: PhysicsBlindFixtureCatalogV1,
    workspace: Path,
    *,
    case_id: str,
    variant_id: str,
    repetition_id: int,
    attempt_number: int,
    mode: Literal["defective", "clean", "malformed", "infrastructure"],
) -> BoundRun:
    name = f"{case_id}-{variant_id}-rep{repetition_id}-attempt{attempt_number}-{mode}"
    run_root = root / name
    run_root.mkdir()
    oracle_catalog = _oracle_catalog(workspace, run_root / "oracle-catalog.json")
    evidence = run_root / "oracle-evidence"
    evidence.mkdir()
    run_physics_oracle(
        catalog_path=oracle_catalog,
        contract_path=workspace / "contract.yaml",
        oracle_id="raw_measurement_oracle",
        task_id=case_id,
        workspace=workspace,
        output_directory=evidence / "raw_measurement_oracle",
        attempt_number=attempt_number,
    )
    fake = _fake_codex(run_root / "fake-codex", mode=mode)
    config = _execution_config(run_root / "execution-config.json", fake)
    codex_home = run_root / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}\n", encoding="ascii")
    output = run_root / "pa3-output"
    pair = catalog.pair(case_id)
    test_environment = {"CODEX_HOME": str(codex_home), "PATH": "/usr/bin:/bin"}
    run_physics_auditor(
        contract_path=workspace / "contract.yaml",
        execution_config_path=config,
        task_id=case_id,
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=output,
        attempt_number=attempt_number,
        environ=test_environment,
        test_qualified_codex=build_test_qualified_physics_auditor_codex(
            fake,
            codex_home,
            environ=test_environment,
        ),
        blindness_authority=BlindBenchmarkLaunchAuthority(
            catalog=catalog,
            pair=pair,
            variant_id=variant_id,
            repository_root=ROOT,
        ),
    )
    artifacts = ExactBenchmarkRunArtifacts(
        case_id=case_id,
        variant_id=variant_id,
        repetition_id=repetition_id,
        contract_path=workspace / "contract.yaml",
        execution_config_path=config,
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=output,
        attempt_number=attempt_number,
    )
    return BoundRun(
        identity=bind_exact_benchmark_run(catalog, artifacts, repository_root=ROOT),
        artifacts=artifacts,
    )


@pytest.fixture(scope="module")
def bound_suite(tmp_path_factory: pytest.TempPathFactory) -> BoundSuite:
    root = tmp_path_factory.mktemp("exact-scoring")
    catalog = load_blind_fixture_catalog(CATALOG_PATH)
    case_one_defective = _git_workspace(root, catalog, "case_001:variant_001")
    case_one_clean = _git_workspace(root, catalog, "case_001:variant_002")
    case_two = _git_workspace(root, catalog, "case_002:variant_001")
    return BoundSuite(
        catalog=catalog,
        defective_one=_run(
            root,
            catalog,
            case_one_defective,
            case_id="case_001",
            variant_id="variant_001",
            repetition_id=1,
            attempt_number=1,
            mode="defective",
        ),
        defective_two=_run(
            root,
            catalog,
            case_one_defective,
            case_id="case_001",
            variant_id="variant_001",
            repetition_id=2,
            attempt_number=2,
            mode="defective",
        ),
        clean=_run(
            root,
            catalog,
            case_one_clean,
            case_id="case_001",
            variant_id="variant_002",
            repetition_id=1,
            attempt_number=3,
            mode="clean",
        ),
        malformed=_run(
            root,
            catalog,
            case_one_clean,
            case_id="case_001",
            variant_id="variant_002",
            repetition_id=2,
            attempt_number=4,
            mode="malformed",
        ),
        infrastructure=_run(
            root,
            catalog,
            case_two,
            case_id="case_002",
            variant_id="variant_001",
            repetition_id=1,
            attempt_number=5,
            mode="infrastructure",
        ),
    )


def _score(suite: BoundSuite, runs: tuple[BoundRun, ...]) -> Any:
    expected = issue_expected_run_manifest(suite.catalog, tuple(item.identity for item in runs))
    return score_exact_physics_benchmark(
        expected,
        tuple(item.observed() for item in runs),
        catalog_path=CATALOG_PATH,
        repository_root=ROOT,
    )


def _identity_with(
    identity: ExactBenchmarkRunIdentityV1,
    **changes: object,
) -> ExactBenchmarkRunIdentityV1:
    return ExactBenchmarkRunIdentityV1.model_validate(
        {**identity.model_dump(mode="json"), **changes}
    )


def test_exact_identity_bijection_and_noncollapsed_semantic_scoring(
    bound_suite: BoundSuite,
) -> None:
    report = _score(
        bound_suite,
        (bound_suite.defective_one, bound_suite.defective_two, bound_suite.clean),
    )

    assert report.exact_run_identity_bijection is True
    assert report.all_runs_proof_verified is True
    assert report.aggregate.run_count == 3
    assert report.aggregate.defect_category_recognition.rate == 1.0
    assert report.aggregate.severity_correctness.rate == 1.0
    assert report.aggregate.route_correctness.rate == 1.0
    assert report.aggregate.required_categories.rate == 1.0
    assert report.aggregate.acceptable_alternatives.rate is None
    assert report.aggregate.forbidden_categories.rate == 1.0
    assert report.aggregate.forbidden_routes.rate == 1.0
    assert report.aggregate.evidence_validity.rate == 1.0
    assert report.aggregate.clean_case_pass.rate == 1.0
    identity = bound_suite.defective_one.identity
    assert identity.pa2_proof_identities
    assert identity.auditor_report_sha256 is not None
    assert identity.deterministic_route == "request_repair"
    assert identity.finding_category_set == ("violated_identity",)
    assert identity.finding_severities[0].severity == "high"
    assert identity.evidence_references


def test_malformed_report_and_infrastructure_failure_are_separate_scores(
    bound_suite: BoundSuite,
) -> None:
    report = _score(bound_suite, (bound_suite.malformed, bound_suite.infrastructure))

    assert report.aggregate.malformed_report_count == 1
    assert report.aggregate.infrastructure_failure_count == 1
    malformed, infrastructure = report.run_scores
    assert malformed.malformed_report is True
    assert malformed.infrastructure_failure is False
    assert infrastructure.malformed_report is False
    assert infrastructure.infrastructure_failure is True
    assert malformed.evidence_validity == "not_applicable"
    assert infrastructure.evidence_validity == "not_applicable"


@pytest.mark.parametrize("mutation", ["missing", "extra", "unrelated", "duplicate"])
def test_expected_observed_key_set_must_be_exact(
    bound_suite: BoundSuite,
    mutation: str,
) -> None:
    runs = (bound_suite.defective_one, bound_suite.defective_two)
    expected = issue_expected_run_manifest(
        bound_suite.catalog,
        tuple(item.identity for item in runs),
    )
    observed = [item.observed() for item in runs]
    unrelated_identity = _identity_with(
        bound_suite.defective_two.identity,
        repetition_id=99,
        pa3_action_id="unrelated-unique-action",
        pa3_action_proof_sha256="9" * 64,
        pa2_proof_identities=[
            {
                **item.model_dump(mode="json"),
                "completion_proof_id": "unrelated-unique-pa2",
                "completion_proof_sha256": "8" * 64,
            }
            for item in bound_suite.defective_two.identity.pa2_proof_identities
        ],
    )
    if mutation == "missing":
        observed.pop()
    elif mutation == "extra":
        observed.append(ExactBenchmarkObservedRun(unrelated_identity, runs[1].artifacts))
    elif mutation == "unrelated":
        observed[1] = ExactBenchmarkObservedRun(unrelated_identity, runs[1].artifacts)
    else:
        observed.append(runs[0].observed())

    with pytest.raises(PhysicsBenchmarkScoringIntegrityError):
        score_exact_physics_benchmark(
            expected,
            tuple(observed),
            catalog_path=CATALOG_PATH,
            repository_root=ROOT,
        )


def test_reused_pa2_or_pa3_proof_cannot_define_a_new_repetition(
    bound_suite: BoundSuite,
) -> None:
    duplicate = _identity_with(
        bound_suite.defective_one.identity,
        repetition_id=77,
        pa3_action_id="unique-but-pa2-reused",
        pa3_action_proof_sha256="7" * 64,
    )

    with pytest.raises(ValidationError, match="reuse"):
        issue_expected_run_manifest(
            bound_suite.catalog,
            (bound_suite.defective_one.identity, duplicate),
        )


def test_swapping_pa3_proofs_between_repetitions_fails_closed(
    bound_suite: BoundSuite,
) -> None:
    first = bound_suite.defective_one
    second = bound_suite.defective_two
    expected = issue_expected_run_manifest(bound_suite.catalog, (first.identity, second.identity))
    swapped_artifacts = replace(
        first.artifacts,
        execution_config_path=second.artifacts.execution_config_path,
        oracle_evidence_root=second.artifacts.oracle_evidence_root,
        output_directory=second.artifacts.output_directory,
        attempt_number=second.artifacts.attempt_number,
    )

    with pytest.raises(PhysicsBenchmarkScoringIntegrityError):
        score_exact_physics_benchmark(
            expected,
            (ExactBenchmarkObservedRun(first.identity, swapped_artifacts), second.observed()),
            catalog_path=CATALOG_PATH,
            repository_root=ROOT,
        )


def test_swapping_pa2_proofs_between_repetitions_fails_closed(
    bound_suite: BoundSuite,
) -> None:
    first = bound_suite.defective_one
    second = bound_suite.defective_two
    expected = issue_expected_run_manifest(bound_suite.catalog, (first.identity, second.identity))
    swapped_pa2 = replace(
        first.artifacts,
        oracle_evidence_root=second.artifacts.oracle_evidence_root,
    )

    with pytest.raises(PhysicsBenchmarkScoringIntegrityError):
        score_exact_physics_benchmark(
            expected,
            (ExactBenchmarkObservedRun(first.identity, swapped_pa2), second.observed()),
            catalog_path=CATALOG_PATH,
            repository_root=ROOT,
        )


def test_swapping_pa3_proofs_between_cases_fails_closed(bound_suite: BoundSuite) -> None:
    first = bound_suite.defective_one
    other = bound_suite.infrastructure
    expected = issue_expected_run_manifest(bound_suite.catalog, (first.identity, other.identity))
    crossed = replace(
        first.artifacts,
        contract_path=other.artifacts.contract_path,
        execution_config_path=other.artifacts.execution_config_path,
        workspace=other.artifacts.workspace,
        oracle_evidence_root=other.artifacts.oracle_evidence_root,
        output_directory=other.artifacts.output_directory,
        attempt_number=other.artifacts.attempt_number,
    )

    with pytest.raises(PhysicsBenchmarkScoringIntegrityError):
        score_exact_physics_benchmark(
            expected,
            (ExactBenchmarkObservedRun(first.identity, crossed), other.observed()),
            catalog_path=CATALOG_PATH,
            repository_root=ROOT,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("finding_category_set", ["sign_or_normalization_error"]),
        (
            "finding_severities",
            [
                {
                    "finding_id": "finding_primary",
                    "category": "violated_identity",
                    "severity": "medium",
                    "status": "open",
                }
            ],
        ),
    ],
)
def test_claimed_findings_or_severity_cannot_diverge_from_bound_report(
    bound_suite: BoundSuite,
    field: str,
    value: object,
) -> None:
    original = bound_suite.defective_one
    payload = original.identity.model_dump(mode="json")
    if field == "finding_category_set":
        payload["finding_severities"][0]["category"] = "sign_or_normalization_error"
    payload[field] = value
    changed = ExactBenchmarkRunIdentityV1.model_validate(payload)
    expected = issue_expected_run_manifest(bound_suite.catalog, (original.identity,))

    with pytest.raises(PhysicsBenchmarkScoringIntegrityError):
        score_exact_physics_benchmark(
            expected,
            (ExactBenchmarkObservedRun(changed, original.artifacts),),
            catalog_path=CATALOG_PATH,
            repository_root=ROOT,
        )


@pytest.mark.parametrize("mutation", ["findings", "severity", "summary"])
def test_report_alteration_after_proof_creation_fails_closed(
    bound_suite: BoundSuite,
    tmp_path: Path,
    mutation: str,
) -> None:
    original = bound_suite.defective_one
    copied_output = tmp_path / "copied-output"
    shutil.copytree(original.artifacts.output_directory, copied_output)
    report_path = copied_output / "physics-audit-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if mutation == "findings":
        report["findings"][0]["category"] = "sign_or_normalization_error"
    elif mutation == "severity":
        report["findings"][0]["severity"] = "medium"
    else:
        report["summary"] = "The report was altered after proof creation."
    report_path.write_bytes(canonical_json(report))
    changed = replace(original.artifacts, output_directory=copied_output)

    with pytest.raises(PhysicsBenchmarkScoringIntegrityError):
        bind_exact_benchmark_run(bound_suite.catalog, changed, repository_root=ROOT)


def test_correct_report_bound_to_wrong_source_or_projection_fails_closed(
    bound_suite: BoundSuite,
    tmp_path: Path,
) -> None:
    original = bound_suite.defective_one
    wrong_source = replace(
        original.artifacts,
        workspace=bound_suite.clean.artifacts.workspace,
        contract_path=bound_suite.clean.artifacts.contract_path,
    )
    with pytest.raises(PhysicsBenchmarkScoringIntegrityError):
        bind_exact_benchmark_run(bound_suite.catalog, wrong_source, repository_root=ROOT)

    copied_output = tmp_path / "projection-output"
    shutil.copytree(original.artifacts.output_directory, copied_output)
    projection_path = copied_output / "control/projection-manifest.json"
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    regular = next(item for item in projection["objects"] if item["kind"] == "regular")
    regular["sha256"] = "0" * 64
    projection_path.write_bytes(canonical_json(projection))
    with pytest.raises(PhysicsBenchmarkScoringIntegrityError):
        bind_exact_benchmark_run(
            bound_suite.catalog,
            replace(original.artifacts, output_directory=copied_output),
            repository_root=ROOT,
        )


def test_complete_pa5c1_launch_certificate_is_bound_into_run_identity(
    bound_suite: BoundSuite,
) -> None:
    original = bound_suite.defective_one
    certificate_path = (
        original.artifacts.output_directory / "control/blindness-certificate.json"
    )
    original_bytes = certificate_path.read_bytes()
    original_mode = certificate_path.stat().st_mode & 0o777
    certificate = json.loads(original_bytes)
    try:
        certificate["launch_manifest"]["codex_executable_sha256"] = "0" * 64
        certificate["pa3_launch_manifest_sha256"] = hashlib.sha256(
            canonical_json(certificate["launch_manifest"])
        ).hexdigest()
        certificate_path.chmod(0o600)
        certificate_path.write_bytes(canonical_json(certificate))

        changed_identity = bind_exact_benchmark_run(
            bound_suite.catalog,
            original.artifacts,
            repository_root=ROOT,
        )
        assert (
            changed_identity.pa3_action_proof_sha256
            == original.identity.pa3_action_proof_sha256
        )
        assert (
            changed_identity.pa3_launch_manifest_sha256
            != original.identity.pa3_launch_manifest_sha256
        )
        assert (
            changed_identity.pa5c1_blindness_certificate_sha256
            != original.identity.pa5c1_blindness_certificate_sha256
        )

        expected = issue_expected_run_manifest(bound_suite.catalog, (original.identity,))
        with pytest.raises(PhysicsBenchmarkScoringIntegrityError, match="not produced"):
            score_exact_physics_benchmark(
                expected,
                (original.observed(),),
                catalog_path=CATALOG_PATH,
                repository_root=ROOT,
            )
    finally:
        certificate_path.chmod(0o600)
        certificate_path.write_bytes(original_bytes)
        certificate_path.chmod(original_mode)


def test_catalog_object_must_equal_exact_certified_scorer_root_catalog(
    bound_suite: BoundSuite,
) -> None:
    payload = bound_suite.catalog.model_dump(mode="json")
    pair = next(item for item in payload["pairs"] if item["case_id"] == "case_002")
    pair["variants"][0]["diagnosis"] = "substituted unrelated catalog authority"
    substituted = PhysicsBlindFixtureCatalogV1.model_validate(payload)

    with pytest.raises(PhysicsBenchmarkScoringIntegrityError, match="supplied catalog differs"):
        bind_exact_benchmark_run(
            substituted,
            bound_suite.defective_one.artifacts,
            repository_root=ROOT,
        )


def test_catalog_path_and_receipts_must_resolve_inside_exact_scorer_root(
    bound_suite: BoundSuite,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    destination = repository / "examples/physics_auditor/benchmark_v1"
    destination.parent.mkdir(parents=True)
    shutil.copytree(BENCHMARK, destination)
    outside_catalog = repository / "outside/catalog.json"
    outside_catalog.parent.mkdir()
    shutil.copy2(destination / "scorer_only/catalog.json", outside_catalog)
    expected = issue_expected_run_manifest(
        bound_suite.catalog,
        (bound_suite.defective_one.identity,),
    )

    with pytest.raises(PhysicsBenchmarkScoringIntegrityError, match="exact catalog"):
        score_exact_physics_benchmark(
            expected,
            (bound_suite.defective_one.observed(),),
            catalog_path=outside_catalog,
            repository_root=repository,
        )

    certified_catalog = destination / "scorer_only/catalog.json"
    catalog_payload = json.loads(certified_catalog.read_text(encoding="utf-8"))
    pair = next(item for item in catalog_payload["pairs"] if item["case_id"] == "case_001")
    original_receipt = repository / pair["receipt_path"]
    outside_receipt = repository / "outside/case_001.json"
    shutil.copy2(original_receipt, outside_receipt)
    pair["receipt_path"] = "outside/case_001.json"
    certified_catalog.write_bytes(canonical_json(catalog_payload))
    changed_catalog = load_blind_fixture_catalog(certified_catalog)

    with pytest.raises(PhysicsBenchmarkScoringIntegrityError, match="review receipt"):
        bind_exact_benchmark_run(
            changed_catalog,
            bound_suite.defective_one.artifacts,
            repository_root=repository,
        )


def test_stale_scorer_authority_fails_closed(
    bound_suite: BoundSuite,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    destination = repository / "examples/physics_auditor/benchmark_v1"
    destination.parent.mkdir(parents=True)
    shutil.copytree(BENCHMARK, destination)
    pair = bound_suite.catalog.pair("case_001")
    receipt_path = repository / pair.receipt_path
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["reviewed_scorer_authority_sha256"] = "0" * 64
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    receipt["receipt_sha256"] = hashlib.sha256(canonical_json(body)).hexdigest()
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(PhysicsBenchmarkScoringIntegrityError, match="stale scorer"):
        bind_exact_benchmark_run(
            bound_suite.catalog,
            bound_suite.defective_one.artifacts,
            repository_root=repository,
        )
