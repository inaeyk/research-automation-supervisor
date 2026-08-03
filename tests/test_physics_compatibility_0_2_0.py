from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Protocol, cast

import yaml  # type: ignore[import-untyped]
from typer.testing import CliRunner

from research_automation_supervisor import __version__
from research_automation_supervisor.cli import app
from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.git_evidence import GitBaseline
from research_automation_supervisor.physics_oracle_models import (
    PhysicsOracleActionRecordV1,
    PhysicsOracleCatalogV1,
    PhysicsOracleCompletionProofV1,
    PhysicsOracleExecutionRequestV1,
    PhysicsOracleExecutionResultV1,
)
from research_automation_supervisor.replay_campaign_models import (
    ReplayCampaignSpecification,
    ReplayCampaignState,
)
from research_automation_supervisor.workflow_engine import (
    JOURNAL_SEMANTIC_FORMS,
    WorkflowServices,
    run_substage,
)
from research_automation_supervisor.workflow_integrity import (
    CodexActionRecord,
    JournalEntry,
    parse_action_record,
)
from research_automation_supervisor.workflow_models import (
    AuditorModelResult,
    HumanFile,
    PendingAction,
    PreparedSubstage,
    PreparedWorkflowTest,
    SubstageSpecification,
    WorkflowState,
    parse_auditor_result,
)
from research_automation_supervisor.workflow_prompts import (
    AUDITOR_OUTPUT_SCHEMA,
    WORKER_OUTPUT_SCHEMA,
    build_initial_worker_prompt,
)
from tests.workflow_helpers import create_workflow_tree

FIXTURES = Path(__file__).parent / "fixtures/compatibility_0_2_0"
PHYSICS_FIXTURES = Path(__file__).parent / "fixtures/physics"
RUNNER = CliRunner()


def _json(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _canonical_model_hash(model: object) -> str:
    value = cast(AnyModel, model).model_dump(mode="json")
    return hashlib.sha256(canonical_json(value)).hexdigest()


class AnyModel(Protocol):
    def model_dump(self, *, mode: str) -> dict[str, object]: ...


def _human_file(path: str, content: str) -> HumanFile:
    value = content.encode("utf-8")
    locator = Path(path)
    return HumanFile(
        locator_path=locator,
        path=locator,
        content=value,
        sha256=hashlib.sha256(value).hexdigest(),
    )


def test_frozen_0_2_0_persisted_models_parse_and_hash_identically() -> None:
    manifest = cast(dict[str, object], _json("compatibility_manifest.json"))
    expected = cast(dict[str, str], manifest["canonical_model_sha256"])
    models: dict[str, object] = {
        "auditor_result.json": parse_auditor_result(
            (FIXTURES / "auditor_result.json").read_bytes()
        ),
        "pending_action.json": PendingAction.model_validate(
            _json("pending_action.json")
        ),
        "codex_action_record.json": parse_action_record(
            _json("codex_action_record.json")
        ),
        "workflow_state.json": WorkflowState.model_validate(
            _json("workflow_state.json")
        ),
        "campaign.yaml": ReplayCampaignSpecification.model_validate(
            yaml.safe_load((FIXTURES / "campaign.yaml").read_text(encoding="utf-8"))
        ),
        "campaign_state.json": ReplayCampaignState.model_validate(
            _json("campaign_state.json")
        ),
        "ordinary_non_physics_substage.yaml": SubstageSpecification.model_validate(
            yaml.safe_load(
                (PHYSICS_FIXTURES / "ordinary_non_physics_substage.yaml").read_text(
                    encoding="utf-8"
                )
            )
        ),
    }

    assert {name: _canonical_model_hash(model) for name, model in models.items()} == expected
    assert isinstance(models["auditor_result.json"], AuditorModelResult)
    assert isinstance(models["codex_action_record.json"], CodexActionRecord)


def test_frozen_0_2_0_model_schemas_have_not_changed() -> None:
    manifest = cast(dict[str, object], _json("compatibility_manifest.json"))
    expected = cast(dict[str, str], manifest["model_schema_sha256"])
    model_types = (
        PendingAction,
        CodexActionRecord,
        WorkflowState,
        JournalEntry,
        SubstageSpecification,
        AuditorModelResult,
        ReplayCampaignSpecification,
        ReplayCampaignState,
    )

    actual = {
        model_type.__name__: hashlib.sha256(
            canonical_json(model_type.model_json_schema())
        ).hexdigest()
        for model_type in model_types
    }

    assert actual == expected
    assert "physics" not in SubstageSpecification.model_fields
    assert "physics" not in WorkflowState.model_fields


def test_frozen_0_2_0_journal_semantic_forms_have_not_been_reinterpreted() -> None:
    fixture = cast(dict[str, object], _json("journal_semantic_forms.json"))
    forms = [
        list(item)
        for item in sorted(
            JOURNAL_SEMANTIC_FORMS,
            key=lambda value: tuple(
                "" if item is None else str(item) for item in value
            ),
        )
    ]

    assert len(forms) == fixture["count"]
    assert hashlib.sha256(canonical_json(forms)).hexdigest() == fixture["canonical_sha256"]
    assert not any("physics" in str(item) for form in forms for item in form)


def test_frozen_0_2_0_workflow_services_surface_is_identical() -> None:
    fixture = cast(dict[str, object], _json("workflow_services.json"))
    services = WorkflowServices()

    assert [field.name for field in dataclasses.fields(WorkflowServices)] == fixture[
        "field_names"
    ]
    assert services.codex_executable == fixture["codex_executable"]
    assert services.codex_invoker.__name__ == fixture[  # type: ignore[attr-defined]
        "codex_invoker"
    ]
    assert services.test_invoker.__name__ == fixture[  # type: ignore[attr-defined]
        "test_invoker"
    ]
    assert services.environ == fixture["environ"]
    assert services.prompt_source == fixture["prompt_source"]
    assert services.require_canonical_thread_ids is fixture[
        "require_canonical_thread_ids"
    ]


def test_frozen_0_2_0_output_schema_hashes_are_identical() -> None:
    manifest = cast(dict[str, object], _json("compatibility_manifest.json"))
    expected = cast(dict[str, str], manifest["workflow_output_schema_sha256"])

    assert {
        "auditor": hashlib.sha256(canonical_json(AUDITOR_OUTPUT_SCHEMA)).hexdigest(),
        "worker": hashlib.sha256(canonical_json(WORKER_OUTPUT_SCHEMA)).hexdigest(),
    } == expected


def test_frozen_0_2_0_initial_worker_prompt_hash_is_identical() -> None:
    manifest = cast(dict[str, object], _json("compatibility_manifest.json"))
    expected = cast(dict[str, object], manifest["initial_worker_prompt"])
    specification_bytes = (
        PHYSICS_FIXTURES / "ordinary_non_physics_substage.yaml"
    ).read_bytes()
    specification = SubstageSpecification.model_validate(
        yaml.safe_load(specification_bytes)
    )
    prepared = PreparedSubstage(
        specification_locator_path=Path("/synthetic/config/substage.yaml"),
        specification_path=Path("/synthetic/config/substage.yaml"),
        specification_bytes=specification_bytes,
        specification_sha256=hashlib.sha256(specification_bytes).hexdigest(),
        specification=specification,
        workspace=Path("/synthetic/project"),
        repository_root=Path("/synthetic/project"),
        baseline_commit="f" * 40,
        baseline_branch="main",
        contract=_human_file(
            "/synthetic/project/control/contract.md",
            "Frozen public synthetic contract.\n",
        ),
        worker_initial_prompt=_human_file(
            "/synthetic/project/control/worker-initial.md",
            "Implement the public synthetic substage.\n",
        ),
        worker_repair_prompt=_human_file(
            "/synthetic/project/control/worker-repair.md",
            "Repair only validated public failures.\n",
        ),
        auditor_prompt=_human_file(
            "/synthetic/project/control/auditor.md",
            "Audit the public synthetic workspace.\n",
        ),
        acceptance_tests=(
            PreparedWorkflowTest(
                specification=specification.acceptance_tests[0],
                cwd=Path("/synthetic/project"),
            ),
        ),
    )
    baseline = GitBaseline(
        workspace="/synthetic/project",
        repository_root="/synthetic/project",
        head="f" * 40,
        branch="main",
        detached=False,
        clean=True,
        status_sha256=hashlib.sha256(b"").hexdigest(),
    )

    rendered = build_initial_worker_prompt(prepared, baseline)

    assert rendered.rendered_sha256 == expected["rendered_sha256"]
    assert rendered.byte_count == expected["byte_count"]


def test_ordinary_0_2_0_workflow_transition_sequence_is_unchanged(
    tmp_path: Path,
) -> None:
    specification, _, fake_codex = create_workflow_tree(tmp_path)

    result = run_substage(
        specification,
        runs_dir=tmp_path / "runs",
        services=WorkflowServices(codex_executable=str(fake_codex)),
    )
    journal = [
        json.loads(line)
        for line in (Path(result.artifact_directory) / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    actual = [
        [item["event_type"], item["previous_state"], item["new_state"], item["reason"]]
        for item in journal
    ]

    assert result.status == "completed"
    assert actual == _json("ordinary_transition_sequence.json")


def test_existing_cli_version_and_nonphysics_validation_remain_unchanged(
    tmp_path: Path,
) -> None:
    specification, _, _ = create_workflow_tree(tmp_path)

    version = RUNNER.invoke(app, ["--version"])
    validation = RUNNER.invoke(app, ["validate-substage", str(specification), "--json"])

    assert __version__ == "0.2.0"
    assert version.exit_code == 0
    assert version.stdout == "0.2.0\n"
    assert validation.exit_code == 0
    payload = json.loads(validation.stdout)
    assert payload["substage_id"] == "minimal-substage"
    assert "physics" not in validation.stdout.casefold()


def test_pa2_execution_models_are_isolated_from_frozen_workflow_types() -> None:
    oracle_types = {
        PhysicsOracleActionRecordV1,
        PhysicsOracleCatalogV1,
        PhysicsOracleCompletionProofV1,
        PhysicsOracleExecutionRequestV1,
        PhysicsOracleExecutionResultV1,
    }
    frozen_types = {
        PendingAction,
        CodexActionRecord,
        WorkflowState,
        JournalEntry,
        SubstageSpecification,
        AuditorModelResult,
        ReplayCampaignSpecification,
        ReplayCampaignState,
    }

    assert oracle_types.isdisjoint(frozen_types)
    assert "physics_oracle" not in WorkflowServices.__dataclass_fields__
    assert all("physics_oracle" not in form for form in JOURNAL_SEMANTIC_FORMS)
