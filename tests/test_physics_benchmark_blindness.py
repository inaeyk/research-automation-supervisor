from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from research_automation_supervisor.codex_adapter import build_codex_command
from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.errors import (
    PhysicsBenchmarkBlindnessInputError,
    PhysicsBenchmarkBlindnessIntegrityError,
)
from research_automation_supervisor.live_shadow_isolation import (
    BubblewrapBackendIdentity,
    BubblewrapCapability,
    build_bubblewrap_process_launch,
)
from research_automation_supervisor.physics_auditor_execution import (
    _persist_accepted_control,
    _persist_prompt_control,
    _prepare_action,
    _prepare_projection_layout,
    _prepared_codex_request,
    build_test_qualified_physics_auditor_codex,
    run_physics_auditor,
)
from research_automation_supervisor.physics_auditor_projection import (
    materialize_physics_auditor_projection,
)
from research_automation_supervisor.physics_benchmark_blindness import (
    BlindBenchmarkLaunchAuthority,
    BlindnessCertificateV1,
    HumanReviewReceiptV1,
    PA3LaunchBindingInputsV1,
    PhysicsBlindFixtureCatalogV1,
    build_gl_visible_manifest,
    build_paired_visible_manifest,
    execute_subject_neutral_raw_oracle,
    issue_blindness_certificate,
    load_blind_fixture_catalog,
    load_human_review_receipt,
    parse_raw_oracle_output,
    persist_blindness_certificate,
    prepare_exact_gl_fixture,
    qualify_fixture_authority,
    validate_generic_raw_oracle_program,
    verify_certified_pa3_launch,
    verify_scorer_root_excluded_from_bubblewrap_command,
)

ROOT = Path(__file__).parents[1]
BENCHMARK = ROOT / "examples/physics_auditor/benchmark_v1"
CATALOG_PATH = BENCHMARK / "scorer_only/catalog.json"
GL_SOURCE = ROOT.parent / "GL-with-AI"
CONFIG = ROOT / "examples/physics_auditor/synthetic/execution-config.yaml"


def _copy_benchmark(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "qualified-repository"
    destination = repository / "examples/physics_auditor/benchmark_v1"
    destination.parent.mkdir(parents=True)
    shutil.copytree(BENCHMARK, destination)
    return repository, destination / "scorer_only/catalog.json"


def _receipt_payload(
    *,
    subject_id: str,
    manifest_sha256: str,
    scorer_authority_sha256: str,
    fixture_author_ids: tuple[str, ...],
    decision: str = "approved",
) -> dict[str, object]:
    body: dict[str, object] = {
        "decision": decision,
        "fixture_author_ids": list(fixture_author_ids),
        "issued_at": "2026-08-05T12:00:00Z",
        "receipt_id": f"review_{subject_id}_scripted_test",
        "reviewed_visible_manifest_sha256": manifest_sha256,
        "reviewed_scorer_authority_sha256": scorer_authority_sha256,
        "reviewer_id": "scripted_external_human_test_authority",
        "reviewer_kind": "human",
        "schema_version": 1,
        "scientific_review": (
            "Scripted test receipt for exercising exact-manifest and independence checks only."
        ),
        "subject_id": subject_id,
    }
    return {
        **body,
        "receipt_sha256": hashlib.sha256(canonical_json(body)).hexdigest(),
    }


def _write_receipt(
    repository: Path,
    relative: str,
    payload: dict[str, object],
) -> None:
    path = repository / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _approve_all(repository: Path, catalog_path: Path) -> PhysicsBlindFixtureCatalogV1:
    catalog = load_blind_fixture_catalog(catalog_path)
    for pair in catalog.pairs:
        pair_manifest = build_paired_visible_manifest(pair, repository_root=repository)
        _write_receipt(
            repository,
            pair.receipt_path,
            _receipt_payload(
                subject_id=pair.case_id,
                manifest_sha256=pair_manifest.canonical_sha256(),
                scorer_authority_sha256=pair.canonical_sha256(),
                fixture_author_ids=catalog.fixture_author_ids,
            ),
        )
    for task in catalog.gl_tasks:
        gl_manifest = build_gl_visible_manifest(
            task,
            repository_root=repository,
            source_repository_root=GL_SOURCE,
            source_commit=catalog.gl_source_commit,
        )
        _write_receipt(
            repository,
            task.receipt_path,
            _receipt_payload(
                subject_id=task.task_id,
                manifest_sha256=gl_manifest.canonical_sha256(),
                scorer_authority_sha256=task.canonical_sha256(),
                fixture_author_ids=catalog.fixture_author_ids,
            ),
        )
    return catalog


def _git(*args: object, cwd: Path) -> None:
    subprocess.run(
        ("/usr/bin/git", "-C", cwd, *(str(item) for item in args)),
        check=True,
        capture_output=True,
    )


def test_all_paired_fixtures_have_equal_contract_oracle_and_schema() -> None:
    catalog = load_blind_fixture_catalog(CATALOG_PATH)

    assert len(catalog.pairs) == 21
    assert {item.case_id for item in catalog.pairs} == {
        f"case_{index:03d}" for index in range(1, 22)
    }
    labels: list[str] = []
    for pair in catalog.pairs:
        manifest = build_paired_visible_manifest(pair, repository_root=ROOT)
        assert manifest.case_id == pair.case_id
        assert len({item.contract_sha256 for item in (manifest,)}) == 1
        left, right = manifest.variants
        assert {item.path for item in left.objects} == {item.path for item in right.objects}
        assert pair.contract_file not in pair.variable_files
        assert not set(pair.oracle_files) & set(pair.variable_files)
        labels.extend(item.fixture_label for item in pair.variants)
    assert labels.count("clean") == 21
    assert labels.count("defective") == 21


def test_corrected_fixture_authority_is_scientifically_specific_and_neutral() -> None:
    catalog = load_blind_fixture_catalog(CATALOG_PATH)
    expected_fragments = {
        "case_008": "Harmonic potential V(x)=k x^2/2",
        "case_009": "Spherically symmetric Euclidean scalar Laplacian",
        "case_012": "two consecutive error ratios near four",
        "case_016": "statistically compatible fitted exponents",
        "case_020": "q-double-dot + omega-squared q = 0",
    }
    for case_id, fragment in expected_fragments.items():
        pair = catalog.pair(case_id)
        for variant in pair.variants:
            root = ROOT / variant.visible_root
            assert fragment in (root / "contract.yaml").read_text(encoding="utf-8")
    for case_id in ("case_018", "case_020", "case_021"):
        pair = catalog.pair(case_id)
        titles = {
            (ROOT / item.visible_root / pair.title_file).read_bytes() for item in pair.variants
        }
        assert titles == {f"Benchmark fixture {int(case_id[-3:]):03d}\n".encode()}

    case_009 = catalog.pair("case_009")
    case_009_defect = next(
        item for item in case_009.variants if item.fixture_label == "defective"
    )
    assert case_009_defect.acceptable_alternative_categories == (
        "tensor_or_index_error",
        "violated_identity",
    )
    case_009_observations = []
    for variant in case_009.variants:
        visible = ROOT / variant.visible_root
        assert "if r == 0.0:" in (visible / "implementation.py").read_text()
        observations = json.loads((visible / "observations.json").read_bytes())
        case_009_observations.append(observations)
        assert {
            item["name"]: item["value"] for item in observations["measurements"]
        }["laplacian_of_r_squared_at_r0"] == 6.0
    assert [
        item["name"] for item in case_009_observations[0]["measurements"]
    ] == [item["name"] for item in case_009_observations[1]["measurements"]]

    case_018 = catalog.pair("case_018")
    case_018_values: dict[str, dict[str, object]] = {}
    for variant in case_018.variants:
        visible = ROOT / variant.visible_root
        contract = (visible / "contract.yaml").read_text(encoding="utf-8")
        assert "explicit provenance" in contract
        observations = json.loads((visible / "observations.json").read_bytes())
        case_018_values[variant.fixture_label] = {
            item["name"]: item["value"] for item in observations["measurements"]
        }
    assert case_018_values["clean"]["independent_zero_input_response"] == 0.0
    assert case_018_values["clean"]["source_derived_zero_input_response"] is None
    assert case_018_values["defective"]["independent_zero_input_response"] is None
    assert case_018_values["defective"]["source_derived_zero_input_response"] == 0.0


def test_signed_receipts_preserve_old_revisions_and_bind_renewed_approvals() -> None:
    signed_path = ROOT / "PA5C1_HUMAN_REVIEW_DECISIONS_SIGNED_FINAL.md"
    assert hashlib.sha256(signed_path.read_bytes()).hexdigest() == (
        "91a9430b0a9f5351e135da5e6282149fcfa8529456676aa4c1a79065b0c4485c"
    )
    records = {
        subject_id: yaml.safe_load(block)
        for subject_id, _heading, block in re.findall(
            r"^### ((?:case|task)_[0-9]{3}) — (APPROVE|REVISE)\n\n"
            r"```yaml\n(.*?)\n```",
            signed_path.read_text(encoding="utf-8"),
            re.MULTILINE | re.DOTALL,
        )
    }
    assert len(records) == 31
    renewed_path = ROOT / "PA5C1_V2_HUMAN_REVIEW_DECISIONS_SIGNED_FINAL.md"
    assert hashlib.sha256(renewed_path.read_bytes()).hexdigest() == (
        "51113cff66f52afdc502d62e91046999c8755816b9b76951652125da77cad4ba"
    )
    renewed_records = {
        subject_id: yaml.safe_load(block)
        for subject_id, _heading, block in re.findall(
            r"^### ((?:case|task)_[0-9]{3}) v2 — (APPROVE|REVISE|REMOVE)\n\n"
            r"```yaml\n(.*?)\n```",
            renewed_path.read_text(encoding="utf-8"),
            re.MULTILINE | re.DOTALL,
        )
    }
    assert set(renewed_records) == {"case_009", "case_018"}
    catalog = load_blind_fixture_catalog(CATALOG_PATH)
    receipt_root = BENCHMARK / "scorer_only/review_receipts"
    receipts = {
        subject_id: load_human_review_receipt(receipt_root / f"{subject_id}.json")
        for subject_id in records
    }
    assert sum(item.decision == "approved" for item in receipts.values()) == 29
    assert {
        subject_id for subject_id, item in receipts.items() if item.decision == "revise"
    } == {"case_009", "case_018"}
    for subject_id, receipt in receipts.items():
        record = records[subject_id]
        assert receipt.reviewer_id == "inaeyk"
        assert receipt.reviewer_kind == "human"
        assert receipt.issued_at == "2026-08-06T09:19:00Z"
        assert receipt.scientific_review == record["scientific_review"]
        assert receipt.reviewed_visible_manifest_sha256 == record[
            "reviewed_visible_manifest_sha256"
        ]
        assert receipt.reviewed_scorer_authority_sha256 == record[
            "reviewed_scorer_authority_sha256"
        ]
        authority = (
            catalog.pair(subject_id)
            if subject_id.startswith("case_")
            else catalog.gl_task(subject_id)
        )
        if subject_id in {"case_009", "case_018"}:
            assert receipt.reviewed_scorer_authority_sha256 != authority.canonical_sha256()
            renewed = load_human_review_receipt(ROOT / authority.receipt_path)
            renewed_record = renewed_records[subject_id]
            assert renewed.decision == "approved"
            assert renewed.scientific_review == renewed_record["scientific_review"]
            assert renewed.reviewed_visible_manifest_sha256 == renewed_record[
                "reviewed_visible_manifest_sha256"
            ]
            assert renewed.reviewed_scorer_authority_sha256 == authority.canonical_sha256()
        else:
            assert receipt.reviewed_scorer_authority_sha256 == authority.canonical_sha256()


def test_gl_tasks_bind_exact_source_and_do_not_expose_expected_interpretation() -> None:
    catalog = load_blind_fixture_catalog(CATALOG_PATH)

    assert len(catalog.gl_tasks) == 10
    for task in catalog.gl_tasks:
        manifest = build_gl_visible_manifest(
            task,
            repository_root=ROOT,
            source_repository_root=GL_SOURCE,
            source_commit=catalog.gl_source_commit,
        )
        assert any(item.path.startswith("source/") for item in manifest.objects)
        if task.task_id in {"task_006", "task_007", "task_008", "task_009"}:
            visible = b"\n".join(
                path.read_bytes() for path in sorted((ROOT / task.visible_root).iterdir())
            ).lower()
            assert task.expected_interpretation.encode().lower() not in visible
            assert b"expected_interpretation" not in visible
            assert b"expected_route" not in visible


def test_generic_oracle_returns_raw_measurements_only_and_is_identity_independent() -> None:
    catalog = load_blind_fixture_catalog(CATALOG_PATH)
    roots = [ROOT / variant.visible_root for pair in catalog.pairs for variant in pair.variants] + [
        ROOT / task.visible_root for task in catalog.gl_tasks
    ]
    for root in roots:
        program = root / "raw_measurement_oracle.py"
        validate_generic_raw_oracle_program(program.read_bytes())
        execution = execute_subject_neutral_raw_oracle(
            program.read_bytes(),
            (root / "observations.json").read_bytes(),
        )
        output = execution.output
        assert output.measurements
        assert execution.subject_identity_inputs == "absent"
        assert execution.original_fixture_path_mounted is False
        assert execution.catalog_or_scorer_mounted is False

    with pytest.raises(PhysicsBenchmarkBlindnessIntegrityError, match="case or task"):
        validate_generic_raw_oracle_program(
            b"task_id = input()\nprint('measurements' if task_id == 'task_001' else 'status')\n"
        )
    with pytest.raises(PhysicsBenchmarkBlindnessIntegrityError, match="classification"):
        parse_raw_oracle_output(b'{"schema_version":1,"outcome":"passed"}\n')


@pytest.mark.skipif(not Path("/usr/bin/bwrap").is_file(), reason="Bubblewrap unavailable")
def test_real_neutral_oracle_path_hides_all_subject_identity_surfaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "case_777"
    source = ROOT / (
        "examples/physics_auditor/benchmark_v1/auditor_visible/cases/"
        "case_001/variant_001"
    )
    aliased = tmp_path / canary
    aliased.symlink_to(source, target_is_directory=True)
    monkeypatch.setenv("PA5C1_SUBJECT_CANARY", canary)
    adversarial = textwrap.dedent(
        '''\
        import json
        import os
        import sys
        from pathlib import Path

        marker = "ca" + "se_" + ("7" * 3)
        argv = list(getattr(sys, "argv"))
        cwd = getattr(os, "getcwd")()
        environ = dict(getattr(os, "environ"))
        filename = globals()["__file__"]
        path = Path(filename)
        alias = Path("payload-alias")
        alias.symlink_to("/input/payload.json")
        surfaces = [
            *argv,
            cwd,
            filename,
            *(str(item) for item in path.resolve().parents),
            *(f"{name}={value}" for name, value in environ.items()),
            getattr(os, "readlink")("/proc/self/exe"),
            getattr(os, "readlink")("/proc/self/cwd"),
            getattr(os, "readlink")(alias),
            str(alias.resolve()),
            Path("/proc/self/cmdline").read_bytes().decode("utf-8", "replace"),
            Path("/proc/self/environ").read_bytes().decode("utf-8", "replace"),
            Path("/proc/self/mountinfo").read_text(encoding="utf-8"),
            Path("/proc/1/cmdline").read_bytes().decode("utf-8", "replace"),
            Path("/proc/1/environ").read_bytes().decode("utf-8", "replace"),
            Path("/proc/1/mountinfo").read_text(encoding="utf-8"),
        ]
        for directory in (Path("/"), Path("/oracle"), Path("/input"), Path("/work"), Path("/tmp")):
            surfaces.extend(str(item) for item in directory.iterdir())
        recovered = int(any(marker in item for item in surfaces))
        print(json.dumps({
            "schema_version": 1,
            "measurements": [{
                "name": "identity_recovered",
                "value": recovered,
                "unit": "count",
                "uncertainty": None,
            }],
        }, separators=(",", ":"), sort_keys=True))
        '''
    ).encode("utf-8")
    payload = (
        b'{"schema_version":1,"measurements":'
        b'[{"name":"input_value","value":1,"unit":"1","uncertainty":null}]}'
    )

    execution = execute_subject_neutral_raw_oracle(
        adversarial,
        (aliased / "observations.json").read_bytes(),
    )

    assert execution.output.measurements[0].name == "identity_recovered"
    assert execution.output.measurements[0].value == 0
    with pytest.raises(PhysicsBenchmarkBlindnessInputError, match="only sealed"):
        execute_subject_neutral_raw_oracle(  # type: ignore[arg-type]
            aliased / "raw_measurement_oracle.py",
            payload,
        )
    with pytest.raises(PhysicsBenchmarkBlindnessIntegrityError, match="subject identity"):
        execute_subject_neutral_raw_oracle(
            adversarial,
            payload.replace(b"input_value", b"case_777"),
        )


def test_unapproved_or_stale_fixture_receipt_cannot_qualify(tmp_path: Path) -> None:
    repository, catalog_path = _copy_benchmark(tmp_path)
    catalog = load_blind_fixture_catalog(catalog_path)
    pair = catalog.pairs[0]
    (repository / pair.receipt_path).unlink()

    with pytest.raises(PhysicsBenchmarkBlindnessInputError, match="receipt"):
        qualify_fixture_authority(
            catalog_path,
            repository_root=repository,
            source_repository_root=GL_SOURCE,
        )
    manifest = build_paired_visible_manifest(pair, repository_root=repository)
    _write_receipt(
        repository,
        pair.receipt_path,
        _receipt_payload(
            subject_id=pair.case_id,
            manifest_sha256="0" * 64,
            scorer_authority_sha256=pair.canonical_sha256(),
            fixture_author_ids=catalog.fixture_author_ids,
        ),
    )
    with pytest.raises(PhysicsBenchmarkBlindnessIntegrityError, match="exact visible"):
        qualify_fixture_authority(
            catalog_path,
            repository_root=repository,
            source_repository_root=GL_SOURCE,
        )
    assert manifest.canonical_sha256() != "0" * 64


def test_all_exact_manifest_receipts_qualify_without_launch(tmp_path: Path) -> None:
    repository, catalog_path = _copy_benchmark(tmp_path)
    catalog = _approve_all(repository, catalog_path)

    result = qualify_fixture_authority(
        catalog_path,
        repository_root=repository,
        source_repository_root=GL_SOURCE,
    )

    assert len(result.pair_manifests) == 21
    assert len(result.gl_manifests) == 10
    assert set(result.approved_subject_ids) == {
        *(item.case_id for item in catalog.pairs),
        *(item.task_id for item in catalog.gl_tasks),
    }
    assert result.model_launched is False
    assert result.gl_pilot_launched is False


def test_receipt_cannot_self_assert_independence_or_replace_approval() -> None:
    fields = HumanReviewReceiptV1.model_fields
    assert "independent" not in fields
    assert "independent_from_fixture_author" not in fields
    assert "approval" not in fields
    with pytest.raises(ValidationError):
        HumanReviewReceiptV1.model_validate(
            {
                **_receipt_payload(
                    subject_id="case_001",
                    manifest_sha256="1" * 64,
                    scorer_authority_sha256="2" * 64,
                    fixture_author_ids=("same_author",),
                ),
                "independent_from_fixture_author": True,
            }
        )


def test_exact_gl_preparation_never_runs_a_pilot(tmp_path: Path) -> None:
    catalog = load_blind_fixture_catalog(CATALOG_PATH)
    for task in catalog.gl_tasks:
        destination = tmp_path / task.task_id
        manifest = prepare_exact_gl_fixture(
            task,
            repository_root=ROOT,
            source_repository_root=GL_SOURCE,
            source_commit=catalog.gl_source_commit,
            destination=destination,
        )
        assert manifest.subject_id == task.task_id
        for blob in task.source_blobs:
            content = (destination / "source" / blob.path).read_bytes()
            assert hashlib.sha256(content).hexdigest() == blob.sha256
    assert not any(path.name.startswith("action-") for path in tmp_path.rglob("*"))


def test_blindness_certificate_is_prelaunch_and_binds_real_pa3_projection(
    tmp_path: Path,
) -> None:
    repository, catalog_path = _copy_benchmark(tmp_path)
    catalog = _approve_all(repository, catalog_path)
    pair = catalog.pair("case_020")
    variant = pair.variants[0]
    workspace = tmp_path / "workspace"
    shutil.copytree(repository / variant.visible_root, workspace)
    _git("init", "-q", cwd=workspace)
    _git("config", "user.name", "Blindness Test", cwd=workspace)
    _git("config", "user.email", "blindness@example.invalid", cwd=workspace)
    _git("add", ".", cwd=workspace)
    _git("commit", "-qm", "fixture", cwd=workspace)
    evidence = tmp_path / "oracle-evidence"
    evidence.mkdir()
    prepared = _prepare_action(
        contract_path=workspace / "contract.yaml",
        execution_config_path=CONFIG,
        task_id=pair.case_id,
        workspace=workspace,
        oracle_evidence_root=evidence,
        action_id="blindness-test-action",
        attempt_number=1,
    )
    action = tmp_path / "action"
    action.mkdir()
    _persist_accepted_control(action, prepared)
    _prepare_projection_layout(action)
    _persist_prompt_control(action, prepared)
    projection_root = action / "quarantine/workspace"
    materialize_physics_auditor_projection(prepared.projection, projection_root)
    runtime_home = action / "quarantine/codex-home"
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    codex.chmod(0o700)
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="ascii")
    volatile = tmp_path / "volatile-action"
    scratch = volatile / "scratch"
    scratch.mkdir(parents=True)
    final_message = volatile / "final-message.md"
    codex_prepared = _prepared_codex_request(action, prepared)
    schema = action / "decisions/blindness-test-action/output-schema.json"
    semantic = build_codex_command(
        codex_prepared,
        str(codex),
        final_message,
        output_schema=schema,
        skip_git_repo_check=True,
        writable_scratch=scratch,
    )
    capability = BubblewrapCapability(
        identity=BubblewrapBackendIdentity(
            schema_version=1,
            isolation_schema_version=1,
            backend="bubblewrap",
            canonical_bubblewrap_path="/usr/bin/bwrap",
            bubblewrap_version="bubblewrap scripted-test",
            capability_result="passed",
        ),
        authentication_file=auth,
    )
    scorer = repository / catalog.scorer_only_root
    launch = build_bubblewrap_process_launch(
        semantic,
        codex_prepared,
        {"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"},
        final_message,
        schema,
        capability=capability,
        stage4_run_root=action,
        runtime_home=runtime_home,
        forbidden_roots=(workspace, evidence, scorer),
        auditor_scratch=scratch,
    )
    proof_set = [
        item.model_dump(mode="json") for item in prepared.request.oracle_completion_proofs
    ]
    binding_inputs = PA3LaunchBindingInputsV1(
        action_request_sha256=prepared.request.canonical_sha256(),
        execution_config_sha256=prepared.config.canonical_sha256(),
        evidence_index_sha256=prepared.discovered.index.canonical_sha256(),
        oracle_completion_proof_set_sha256=hashlib.sha256(
            canonical_json(proof_set)
        ).hexdigest(),
        workspace_identity_sha256=prepared.initial_identity.canonical_sha256(),
        prompt_sha256=prepared.prompt.rendered_sha256,
        output_schema_sha256=hashlib.sha256(schema.read_bytes()).hexdigest(),
    )

    certificate = issue_blindness_certificate(
        catalog=catalog,
        pair=pair,
        variant_id=variant.variant_id,
        repository_root=repository,
        projection_manifest=prepared.projection.manifest,
        projection_root=projection_root,
        prompt=prepared.prompt.content,
        runtime_home=runtime_home,
        launch=launch,
        semantic_argv=semantic,
        codex_executable=codex,
        execution_config=prepared.config,
        binding_inputs=binding_inputs,
        output_schema=schema,
        bubblewrap_identity=capability.identity,
    )

    assert certificate.validation_phase == "before_model_launch"
    assert certificate.model_launched_during_validation is False
    assert certificate.reviewed_visible_manifest_sha256 == (
        certificate.paired_visible_manifest_sha256
    )
    assert certificate.pa3_launch_manifest_sha256 == (
        certificate.launch_manifest.canonical_sha256()
    )
    assert certificate.launch_manifest.bubblewrap_argv == launch.command
    assert certificate.launch_manifest.scorer_root_mount == "absent"
    verify_certified_pa3_launch(
        certificate,
        catalog=catalog,
        pair=pair,
        variant_id=variant.variant_id,
        repository_root=repository,
        projection_manifest=prepared.projection.manifest,
        projection_root=projection_root,
        prompt=prepared.prompt.content,
        runtime_home=runtime_home,
        launch=launch,
        semantic_argv=semantic,
        codex_executable=codex,
        execution_config=prepared.config,
        binding_inputs=binding_inputs,
        output_schema=schema,
        bubblewrap_identity=capability.identity,
    )
    changed_model = list(launch.command)
    changed_model[changed_model.index("--model") + 1] = "changed-model"
    changed_mount = list(launch.command)
    workspace_mount = next(
        index
        for index, item in enumerate(changed_mount[:-2])
        if item == "--ro-bind" and changed_mount[index + 2] == "/workspace"
    )
    changed_mount[workspace_mount] = "--bind"
    tampered_launches = (
        replace(launch, environment={**launch.environment, "HOME": "/changed"}),
        replace(launch, cwd=Path("/tmp")),
        replace(launch, command=tuple(changed_model)),
        replace(launch, command=tuple(changed_mount)),
    )
    for tampered in tampered_launches:
        with pytest.raises(PhysicsBenchmarkBlindnessIntegrityError):
            verify_certified_pa3_launch(
                certificate,
                catalog=catalog,
                pair=pair,
                variant_id=variant.variant_id,
                repository_root=repository,
                projection_manifest=prepared.projection.manifest,
                projection_root=projection_root,
                prompt=prepared.prompt.content,
                runtime_home=runtime_home,
                launch=tampered,
                semantic_argv=semantic,
                codex_executable=codex,
                execution_config=prepared.config,
                binding_inputs=binding_inputs,
                output_schema=schema,
                bubblewrap_identity=capability.identity,
            )
    certificate_path = tmp_path / "certificate.json"
    persist_blindness_certificate(certificate, certificate_path)
    with pytest.raises(PhysicsBenchmarkBlindnessIntegrityError, match="immutable"):
        persist_blindness_certificate(certificate, certificate_path)
    projected = b"\n".join(
        path.read_bytes() for path in projection_root.rglob("*") if path.is_file()
    )
    assert b"expected_route" not in projected + prepared.prompt.content
    assert b"diagnosis" not in projected + prepared.prompt.content
    assert not (projection_root / catalog.scorer_only_root).exists()

    (runtime_home / "leak.json").write_text('{"expected_route":"pass"}\n')
    with pytest.raises(PhysicsBenchmarkBlindnessIntegrityError, match="runtime home"):
        issue_blindness_certificate(
            catalog=catalog,
            pair=pair,
            variant_id=variant.variant_id,
            repository_root=repository,
            projection_manifest=prepared.projection.manifest,
            projection_root=projection_root,
            prompt=prepared.prompt.content,
            runtime_home=runtime_home,
            launch=launch,
            semantic_argv=semantic,
            codex_executable=codex,
            execution_config=prepared.config,
            binding_inputs=binding_inputs,
            output_schema=schema,
            bubblewrap_identity=capability.identity,
        )


def test_real_pa3_bubblewrap_builder_omits_scorer_root(tmp_path: Path) -> None:
    repository, catalog_path = _copy_benchmark(tmp_path)
    catalog = _approve_all(repository, catalog_path)
    pair = catalog.pair("case_001")
    variant = pair.variants[0]
    workspace = tmp_path / "workspace"
    shutil.copytree(repository / variant.visible_root, workspace)
    _git("init", "-q", cwd=workspace)
    _git("config", "user.name", "Namespace Test", cwd=workspace)
    _git("config", "user.email", "namespace@example.invalid", cwd=workspace)
    _git("add", ".", cwd=workspace)
    _git("commit", "-qm", "fixture", cwd=workspace)
    evidence = tmp_path / "oracle-evidence"
    evidence.mkdir()
    prepared = _prepare_action(
        contract_path=workspace / "contract.yaml",
        execution_config_path=CONFIG,
        task_id=pair.case_id,
        workspace=workspace,
        oracle_evidence_root=evidence,
        action_id="namespace-test-action",
        attempt_number=1,
    )
    action = tmp_path / "action"
    action.mkdir()
    _persist_accepted_control(action, prepared)
    _prepare_projection_layout(action)
    _persist_prompt_control(action, prepared)
    materialize_physics_auditor_projection(
        prepared.projection,
        action / "quarantine/workspace",
    )
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    codex.chmod(0o700)
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="ascii")
    volatile = tmp_path / "volatile-action"
    scratch = volatile / "scratch"
    scratch.mkdir(parents=True)
    final_message = volatile / "final-message.md"
    codex_prepared = _prepared_codex_request(action, prepared)
    schema = action / "decisions/namespace-test-action/output-schema.json"
    semantic = build_codex_command(
        codex_prepared,
        str(codex),
        final_message,
        output_schema=schema,
        skip_git_repo_check=True,
        writable_scratch=scratch,
    )
    capability = BubblewrapCapability(
        identity=BubblewrapBackendIdentity(
            schema_version=1,
            isolation_schema_version=1,
            backend="bubblewrap",
            canonical_bubblewrap_path="/usr/bin/bwrap",
            bubblewrap_version="bubblewrap scripted-test",
            capability_result="passed",
        ),
        authentication_file=auth,
    )
    scorer = repository / catalog.scorer_only_root
    launch = build_bubblewrap_process_launch(
        semantic,
        codex_prepared,
        {"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"},
        final_message,
        schema,
        capability=capability,
        stage4_run_root=action,
        runtime_home=action / "quarantine/codex-home",
        forbidden_roots=(workspace, evidence, scorer),
        auditor_scratch=scratch,
    )

    verify_scorer_root_excluded_from_bubblewrap_command(
        launch.command,
        scorer_root=scorer,
    )
    assert str(scorer) not in launch.command


@pytest.mark.skipif(not Path("/usr/bin/bwrap").is_file(), reason="Bubblewrap unavailable")
def test_real_pa3_exec_verifies_exact_certificate_before_scripted_process_start(
    tmp_path: Path,
) -> None:
    repository, catalog_path = _copy_benchmark(tmp_path)
    catalog = _approve_all(repository, catalog_path)
    pair = catalog.pair("case_001")
    variant = pair.variants[0]
    workspace = tmp_path / "workspace"
    shutil.copytree(repository / variant.visible_root, workspace)
    _git("init", "-q", cwd=workspace)
    _git("config", "user.name", "Certified Launch Test", cwd=workspace)
    _git("config", "user.email", "certified-launch@example.invalid", cwd=workspace)
    _git("add", ".", cwd=workspace)
    _git("commit", "-qm", "fixture", cwd=workspace)
    evidence = tmp_path / "oracle-evidence"
    evidence.mkdir()
    fake = tmp_path / "scripted-codex"
    fake.write_text(
        textwrap.dedent(
            '''\
            #!/usr/bin/python3
            import json
            import sys
            from pathlib import Path

            sys.stdin.buffer.read()
            index = sys.argv.index("--output-last-message")
            Path(sys.argv[index + 1]).write_text("{}", encoding="ascii")
            print(json.dumps({"type": "thread.started", "thread_id": "certified-fake"}))
            print(json.dumps({
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1,
                    "cached_input_tokens": 0,
                    "output_tokens": 1,
                    "reasoning_output_tokens": 0,
                },
            }))
            '''
        ),
        encoding="ascii",
    )
    fake.chmod(0o700)
    config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    config["trusted_executable"] = {
        "path": str(fake),
        "sha256": hashlib.sha256(fake.read_bytes()).hexdigest(),
    }
    config_path = tmp_path / "execution-config.json"
    config_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    auth_home = tmp_path / "codex-home"
    auth_home.mkdir()
    (auth_home / "auth.json").write_text("{}\n", encoding="ascii")
    output = tmp_path / "audit-output"
    certificate_path = output / "control/blindness-certificate.json"
    checkpoints: list[str] = []

    def checkpoint(name: str) -> None:
        checkpoints.append(name)
        if name == "model_running":
            assert certificate_path.is_file()

    test_environment = {"CODEX_HOME": str(auth_home), "PATH": "/usr/bin:/bin"}
    result = run_physics_auditor(
        contract_path=workspace / "contract.yaml",
        execution_config_path=config_path,
        task_id=pair.case_id,
        workspace=workspace,
        oracle_evidence_root=evidence,
        output_directory=output,
        action_id="certified-launch-test",
        environ=test_environment,
        test_qualified_codex=build_test_qualified_physics_auditor_codex(
            fake,
            auth_home,
            environ=test_environment,
        ),
        blindness_authority=BlindBenchmarkLaunchAuthority(
            catalog=catalog,
            pair=pair,
            variant_id=variant.variant_id,
            repository_root=repository,
        ),
        checkpoint=checkpoint,
    )

    assert "model_running" in checkpoints
    assert result.failure_reason == "invalid_structured_output"
    certificate = BlindnessCertificateV1.model_validate_json(
        certificate_path.read_bytes()
    )
    assert certificate.validation_phase == "before_model_launch"
    assert certificate.launch_manifest.bubblewrap_argv[0] == "/usr/bin/bwrap"
    assert certificate.launch_manifest.source_workspace_mount == "absent"
    assert certificate.launch_manifest.oracle_evidence_mount == "absent"
    assert certificate.launch_manifest.scorer_root_mount == "absent"


def test_runtime_symlink_path_and_proc_escape_attempts_fail_closed(tmp_path: Path) -> None:
    repository, catalog_path = _copy_benchmark(tmp_path)
    catalog = load_blind_fixture_catalog(catalog_path)
    pair = catalog.pair("case_001")
    escaped = repository / pair.variants[0].visible_root / "implementation.py"
    escaped.unlink()
    escaped.symlink_to(ROOT / "README.md")
    with pytest.raises(PhysicsBenchmarkBlindnessInputError, match="single-link"):
        build_paired_visible_manifest(pair, repository_root=repository)

    raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="paths"):
        PhysicsBlindFixtureCatalogV1.model_validate({**raw, "auditor_visible_root": "../escape"})

    scorer = ROOT / catalog.scorer_only_root
    safe = (
        "/usr/bin/bwrap",
        "--unshare-pid",
        "--proc",
        "/proc",
        "--ro-bind",
        "/usr",
        "/usr",
    )
    verify_scorer_root_excluded_from_bubblewrap_command(safe, scorer_root=scorer)
    with pytest.raises(PhysicsBenchmarkBlindnessIntegrityError, match="scorer"):
        verify_scorer_root_excluded_from_bubblewrap_command(
            (*safe, "--ro-bind", str(scorer), "/workspace/scorer"),
            scorer_root=scorer,
        )
    with pytest.raises(PhysicsBenchmarkBlindnessIntegrityError, match="host proc"):
        verify_scorer_root_excluded_from_bubblewrap_command(
            (*safe, "--ro-bind", "/proc", "/proc"),
            scorer_root=scorer,
        )
