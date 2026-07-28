from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from research_automation_supervisor.errors import (
    LiveShadowInputError,
    LiveShadowIntegrityError,
    LiveShadowStateError,
    WorkflowLockError,
)
from research_automation_supervisor.live_shadow_engine import (
    abort_live_shadow,
    live_shadow_exit_code,
    live_shadow_report,
    live_shadow_status,
    record_live_shadow_review,
    resume_live_shadow,
    run_live_shadow,
)
from research_automation_supervisor.workflow_engine import (
    WorkflowServices,
    continue_substage,
    run_substage,
    substage_status,
)
from tests.live_shadow_helpers import (
    create_live_shadow_tree,
    live_supervisor_response,
)
from tests.shadow_helpers import (
    SOURCE_AUDITOR_UUID,
    SOURCE_WORKER_UUID,
    write_review,
)
from tests.workflow_helpers import (
    auditor_result,
    codex_response,
    git,
    worker_result,
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _without_runtime_locators(value: object, run_directory: Path) -> object:
    if isinstance(value, dict):
        return {
            key: _without_runtime_locators(item, run_directory)
            for key, item in value.items()
            if key
            not in {
                "started_at",
                "ended_at",
                "updated_at",
                "duration_seconds",
            }
        }
    if isinstance(value, list):
        return [
            _without_runtime_locators(item, run_directory)
            for item in value
        ]
    if isinstance(value, str):
        return value.replace(str(run_directory), "<STAGE2_RUN>")
    return value


def _authoritative_equivalence_evidence(
    run_directory: Path,
    auditor_prompt: bytes,
) -> dict[str, object]:
    action_ids = ("worker-r000", "auditor-r000")
    normalized_auditor_prompt = auditor_prompt.replace(
        str(run_directory).encode("utf-8"),
        b"<STAGE2_RUN>",
    )
    normalized_auditor_prompt = re.sub(
        rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z",
        b"<TIMESTAMP>",
        normalized_auditor_prompt,
    )
    normalized_auditor_prompt = re.sub(
        rb'("duration_seconds":)-?\d+(?:\.\d+)?',
        rb"\g<1>0",
        normalized_auditor_prompt,
    )
    normalized_auditor_sha256 = hashlib.sha256(
        normalized_auditor_prompt
    ).hexdigest()
    stage1_directories = {
        "worker-r000": run_directory / "worker/codex/worker-r000",
        "auditor-r000": run_directory / "audits/codex/auditor-r000",
    }
    handoffs = {
        action_id: _read_json(
            run_directory / "handoffs" / f"{action_id}.json"
        )
        for action_id in action_ids
    }
    requests = {
        action_id: _without_runtime_locators(
            _read_json(directory / "request.normalized.json"),
            run_directory,
        )
        for action_id, directory in stage1_directories.items()
    }
    metadata: dict[str, object] = {}
    for action_id, directory in stage1_directories.items():
        raw = _read_json(directory / "metadata.json")
        metadata[action_id] = _without_runtime_locators(
            {
                key: raw[key]
                for key in (
                    "run_id",
                    "role",
                    "workspace",
                    "prompt_path",
                    "prompt_sha256",
                    "prompt_byte_count",
                    "model",
                    "reasoning_effort",
                    "timeout_seconds",
                    "sandbox",
                    "approval_policy",
                    "ephemeral",
                    "command",
                    "removed_environment_variable_names",
                    "codex_executable",
                    "codex_version",
                    "resume_thread_id",
                    "output_schema_path",
                    "output_schema_sha256",
                    "permission_evidence",
                )
            },
            run_directory,
        )
    auditor_metadata = metadata["auditor-r000"]
    assert isinstance(auditor_metadata, dict)
    auditor_metadata["prompt_sha256"] = normalized_auditor_sha256
    auditor_metadata["prompt_byte_count"] = len(normalized_auditor_prompt)
    journal = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    intents = {
        entry["action_id"]: _without_runtime_locators(
            entry["state_updates"]["pending_action"],
            run_directory,
        )
        for entry in journal
        if entry["event_type"] == "action_intent"
    }
    auditor_intent = intents["auditor-r000"]
    assert isinstance(auditor_intent, dict)
    auditor_intent["prompt_sha256"] = normalized_auditor_sha256
    auditor_intent.pop("handoff_sha256")
    state = _read_json(run_directory / "state.json")
    result = _read_json(run_directory / "result.json")
    normalized_spec = _read_json(run_directory / "spec.normalized.json")
    git_evidence = _without_runtime_locators(
        _read_json(Path(str(state["latest_git_evidence_path"]))),
        run_directory,
    )
    test_evidence = _without_runtime_locators(
        _read_json(Path(str(state["latest_tests_path"]))),
        run_directory,
    )
    substantive_results = {
        "worker": _read_json(
            run_directory / "worker/worker-r000.structured.json"
        ),
        "auditor": _read_json(
            run_directory / "audits/auditor-r000.structured.json"
        ),
    }
    final_fields = {
        key: result[key]
        for key in (
            "status",
            "pause_reason",
            "repair_round",
            "max_repair_rounds",
            "checkpoint_after",
            "tests_passed",
            "scope_compliant",
            "contract_satisfied",
            "latest_worker_action_id",
            "latest_audit_action_id",
        )
    }
    return {
        "rendered_prompt_hashes": {
            "worker-r000": handoffs["worker-r000"][
                "rendered_prompt_sha256"
            ],
            "auditor-r000": normalized_auditor_sha256,
        },
        "prepared_normalized_requests": requests,
        "codex_policy_metadata": metadata,
        "removed_environment_variable_names": {
            action_id: metadata[action_id][
                "removed_environment_variable_names"
            ]
            for action_id in action_ids
        },
        "action_intent_semantics": intents,
        "acceptance_test_definitions": normalized_spec["acceptance_tests"],
        "acceptance_test_results": test_evidence,
        "git_scope_evidence": git_evidence,
        "repair_and_final_status": final_fields,
        "substantive_results": substantive_results,
    }


def test_stage4_authoritative_stage2_matches_an_ordinary_direct_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUDIT_API_KEY", "controlled-audit-secret")
    monkeypatch.setenv(
        "DBUS_SESSION_BUS_ADDRESS",
        "controlled-session-secret",
    )
    spec, stage2_spec, project, fake, services = create_live_shadow_tree(tmp_path)
    git(project, "config", "core.trustctime", "false")
    git(project, "config", "core.checkStat", "minimal")

    # Preserve an identical second workspace/specification instance, then put it
    # back at the same ordinary locators so path bytes do not muddy hash parity.
    stage2_tree = tmp_path / "stage2"
    pristine_second_instance = tmp_path / "stage2-pristine-second-instance"
    shutil.copytree(stage2_tree, pristine_second_instance)
    direct = run_substage(
        stage2_spec,
        runs_dir=tmp_path / "direct-stage2-runs",
        services=WorkflowServices(
            codex_executable=str(fake),
            token_factory=lambda: "direct-equivalence-token",
        ),
    )
    consumed_first_instance = tmp_path / "stage2-direct-first-instance"
    stage2_tree.rename(consumed_first_instance)
    shutil.copytree(pristine_second_instance, stage2_tree)

    live = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "live-stage2-runs",
        services=services,
    )
    assert direct.status == "completed"
    assert live.authoritative_stage2_status == "completed"
    direct_observation = _read_json(
        consumed_first_instance / "fake-observation.json"
    )
    live_observation = _read_json(stage2_tree / "fake-observation.json")
    direct_evidence = _authoritative_equivalence_evidence(
        Path(direct.artifact_directory),
        base64.b64decode(str(direct_observation["prompt_base64"])),
    )
    live_evidence = _authoritative_equivalence_evidence(
        Path(str(live.authoritative_stage2_run)),
        base64.b64decode(str(live_observation["prompt_base64"])),
    )
    assert direct_evidence == live_evidence
    for removed_names in live_evidence[
        "removed_environment_variable_names"
    ].values():
        assert {
            "AUDIT_API_KEY",
            "DBUS_SESSION_BUS_ADDRESS",
        }.issubset(removed_names)


def test_live_run_preserves_authority_and_quarantines_two_proposals(
    tmp_path: Path,
) -> None:
    spec, _, project, _, services = create_live_shadow_tree(tmp_path)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    assert result.status == "awaiting_reviews"
    assert result.authoritative_stage2_status == "completed"
    assert result.observed_decision_count == 2
    assert result.proposal_count == 2
    assert result.comparison_count == 2
    assert result.review_count == 0
    assert result.automation_enabled is False
    run_directory = Path(result.artifact_directory)
    authoritative = substage_status(
        Path(result.authoritative_stage2_run or "")
    )
    assert authoritative.status == "completed"
    quarantine = run_directory / "quarantine"
    assert {path.name for path in quarantine.iterdir()} == {
        "codex-home",
        "workspace",
    }
    assert not tuple((quarantine / "workspace").iterdir())
    observations = json.loads(
        (tmp_path / "live-shadow-observation.json").read_text(encoding="utf-8")
    )
    assert observations["cwd"] == str(quarantine / "workspace")
    prompt = base64.b64decode(observations["prompt_base64"])
    assert str(project).encode("utf-8") not in prompt
    assert str(result.authoritative_stage2_run).encode("utf-8") not in prompt
    assert (run_directory / "decisions/worker_initial-r000-a001/envelope.json").is_file()
    assert (run_directory / "comparisons/auditor-r000-a002/comparison.json").is_file()
    for proposal_id, resumed in (
        ("worker_initial-r000-a001", False),
        ("auditor-r000-a002", True),
    ):
        stage1 = run_directory / "proposals" / proposal_id / "stage1-run"
        normalized = _read_json(stage1 / "request.normalized.json")
        metadata = _read_json(stage1 / "metadata.json")
        command = metadata["command"]
        exec_index = command.index("exec")
        assert normalized["skip_git_repo_check"] is True
        assert command.count("--skip-git-repo-check") == 1
        assert command.index("--skip-git-repo-check") == exec_index + 1
        assert ("resume" in command) is resumed
        if resumed:
            assert command.index("resume") == exec_index + 2
    assert live_shadow_status(run_directory) == result
    report = live_shadow_report(run_directory)
    assert report["automation_enabled"] is False


def test_live_candidates_preserve_authoritative_downstream_capabilities(
    tmp_path: Path,
) -> None:
    exact_command = shlex.join((sys.executable, "tools/acceptance.py"))
    worker_prompt = (
        "Inspect the authoritative workspace and read the existing source, tests, "
        "and frozen contract. Modify only allowed paths under src/**. Run exactly "
        f"`{exact_command}` and report the changed paths and exact result."
    )
    auditor_prompt = (
        "Inspect the authoritative workspace, relevant source, tests, frozen "
        "contract, and complete diff. Do not edit the workspace. Independently "
        f"run exactly `{exact_command}` and perform any additional read-only "
        "checks within the frozen scope. Report concrete findings or PASS."
    )

    worker_response = live_supervisor_response("worker_initial")
    worker_value = json.loads(str(worker_response["final"]))
    worker_value["prompt"] = worker_prompt
    worker_response["final"] = json.dumps(worker_value, sort_keys=True)
    worker_response["observation_path"] = str(
        tmp_path / "worker-shadow-observation.json"
    )
    auditor_response = live_supervisor_response("auditor", resume=True)
    auditor_value = json.loads(str(auditor_response["final"]))
    auditor_value["prompt"] = auditor_prompt
    auditor_response["final"] = json.dumps(auditor_value, sort_keys=True)
    auditor_response["observation_path"] = str(
        tmp_path / "auditor-shadow-observation.json"
    )

    spec, _, project, _, services = create_live_shadow_tree(
        tmp_path,
        supervisor_responses=[worker_response, auditor_response],
    )
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    candidates = {
        "worker_initial": (
            run_directory
            / "proposals/worker_initial-r000-a001/candidate-prompt.md"
        ).read_text(encoding="utf-8"),
        "auditor": (
            run_directory / "proposals/auditor-r000-a002/candidate-prompt.md"
        ).read_text(encoding="utf-8"),
    }

    assert "inspect the authoritative workspace" in candidates["worker_initial"].lower()
    assert "source, tests, and frozen contract" in candidates["worker_initial"].lower()
    assert "modify only allowed paths" in candidates["worker_initial"].lower()
    assert exact_command in candidates["worker_initial"]
    assert "report the changed paths and exact result" in candidates["worker_initial"]
    assert "inspect the authoritative workspace" in candidates["auditor"].lower()
    assert "complete diff" in candidates["auditor"].lower()
    assert exact_command in candidates["auditor"]
    assert "independently" in candidates["auditor"].lower()
    assert "additional read-only checks" in candidates["auditor"].lower()
    assert "findings or PASS" in candidates["auditor"]
    assert "do not edit the workspace" in candidates["auditor"].lower()

    supervisor_only_restrictions = (
        "use only the supplied evidence",
        "use only the frozen evidence",
        "without inspecting the live repository",
        "do not inspect the live repository",
        "do not request or perform execution",
        "rely on the recorded passing test",
    )
    for candidate in candidates.values():
        for restriction in supervisor_only_restrictions:
            assert restriction not in candidate.lower()

    worker_result_value = _read_json(
        run_directory
        / "proposals/worker_initial-r000-a001/supervisor-result.json"
    )
    auditor_result_value = _read_json(
        run_directory / "proposals/auditor-r000-a002/supervisor-result.json"
    )
    assert worker_result_value["referenced_paths"] == ["src/output.txt"]
    assert auditor_result_value["referenced_paths"] == []
    assert auditor_result_value["permission_change_requested"] is False

    worker_envelope = _read_json(
        run_directory / "decisions/worker_initial-r000-a001/envelope.json"
    )
    auditor_envelope = _read_json(
        run_directory / "decisions/auditor-r000-a002/envelope.json"
    )
    worker_action = worker_envelope["triggering_evidence"]["downstream_action"]
    auditor_action = auditor_envelope["triggering_evidence"]["downstream_action"]
    assert worker_action["role"] == "worker"
    assert worker_action["workspace_inspection"] is True
    assert worker_action["workspace_editing"] == "allowed_paths_only"
    assert worker_action["acceptance_test_execution"] == "exact_argv"
    assert auditor_action["role"] == "auditor"
    assert auditor_action["complete_diff_inspection"] is True
    assert auditor_action["workspace_editing"] is False
    assert (
        auditor_action["acceptance_test_execution"]
        == "independent_exact_argv"
    )

    blind_inputs = tuple(
        base64.b64decode(
            _read_json(tmp_path / observation)["prompt_base64"]
        )
        for observation in (
            "worker-shadow-observation.json",
            "auditor-shadow-observation.json",
        )
    )
    authoritative_prompt_bytes = tuple(
        (project / relative).read_bytes()
        for relative in (
            "control/worker-initial.md",
            "control/worker-repair.md",
            "control/auditor.md",
        )
    )
    for blind_input in blind_inputs:
        for authoritative_prompt in authoritative_prompt_bytes:
            assert authoritative_prompt not in blind_input

    assert result.automation_enabled is False
    assert "delivery" not in {path.name for path in run_directory.iterdir()}
    assert "stage5" not in {path.name for path in run_directory.iterdir()}


def test_exact_earlier_blind_stdin_excludes_every_later_evidence_domain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        domain: f"{domain}-{hashlib.sha256(os.urandom(32)).hexdigest()}"
        for domain in (
            "human-source",
            "current-output",
            "workspace-change",
            "git-evidence",
            "test-evidence",
            "audit-evidence",
            "repair-evidence",
            "comparison",
            "review",
            "future-sentinel",
            "rendered-prompt",
            "state-transition",
        )
    }
    first_worker = json.loads(worker_result())
    first_worker["summary"] = values["current-output"]
    failed_audit = json.loads(auditor_result("fail_repairable"))
    failed_audit["summary"] = values["audit-evidence"]
    failed_audit["findings"][0]["evidence"] = values["audit-evidence"]
    repair_worker = json.loads(worker_result())
    repair_worker["summary"] = values["repair-evidence"]
    first_supervisor = live_supervisor_response("worker_initial")
    first_proposal = json.loads(str(first_supervisor["final"]))
    first_proposal["prompt"] = values["comparison"]
    first_supervisor["final"] = json.dumps(first_proposal, sort_keys=True)
    first_supervisor["observation_path"] = str(
        tmp_path / "earlier-supervisor-observation.json"
    )
    supervisor_responses = [
        first_supervisor,
        live_supervisor_response("auditor", resume=True),
        live_supervisor_response("worker_audit_repair", resume=True),
        live_supervisor_response("auditor", resume=True),
    ]
    first_stage2_observation = tmp_path / "initial-worker-observation.json"
    stage2_responses = [
        codex_response(
            "worker",
            SOURCE_WORKER_UUID,
            json.dumps(first_worker, sort_keys=True),
            observation_path=str(first_stage2_observation),
            write_files={
                f"src/{values['git-evidence']}.txt": (
                    values["workspace-change"]
                )
            },
        ),
        codex_response(
            "auditor",
            SOURCE_AUDITOR_UUID,
            json.dumps(failed_audit, sort_keys=True),
        ),
        codex_response(
            "worker",
            SOURCE_WORKER_UUID,
            json.dumps(repair_worker, sort_keys=True),
            expected_resume_thread_id=SOURCE_WORKER_UUID,
        ),
        codex_response(
            "auditor",
            "55555555-5555-4555-8555-55555555555e",
            auditor_result(),
        ),
    ]
    spec, _, project, _, services = create_live_shadow_tree(
        tmp_path,
        supervisor_responses=supervisor_responses,
        stage2_responses=stage2_responses,
    )
    human_source = (
        values["human-source"] + "\n"
    ).encode("utf-8")
    (project / "control" / "worker-initial.md").write_bytes(human_source)
    (project / "tools" / "acceptance.py").write_text(
        f"print({values['test-evidence']!r})\n",
        encoding="utf-8",
    )
    git(project, "add", "control/worker-initial.md", "tools/acceptance.py")
    git(project, "commit", "-q", "-m", "seed blind-input domain sentinels")

    rendered_sentinel = values["rendered-prompt"].encode("utf-8")
    transition_sentinel = values["state-transition"].encode("utf-8")
    injection_directory = tmp_path / "authoritative-render-injection"
    injection_directory.mkdir()
    injection_source = (
        "import base64\n"
        "import hashlib\n"
        "from dataclasses import replace\n"
        "import research_automation_supervisor.workflow_engine as engine\n"
        "_original = engine.build_initial_worker_prompt\n"
        "_original_update = engine._update_state\n"
        f"_sentinel = base64.b64decode({base64.b64encode(rendered_sentinel)!r})\n"
        f"_transition = base64.b64decode({base64.b64encode(transition_sentinel)!r}).decode()\n"
        "def _seed(prepared, baseline):\n"
        "    rendered = _original(prepared, baseline)\n"
        "    content = rendered.content + b'\\n' + _sentinel + b'\\n'\n"
        "    return replace(rendered, content=content, "
        "rendered_sha256=hashlib.sha256(content).hexdigest(), "
        "byte_count=len(content))\n"
        "def _seed_update(context, reason, updates):\n"
        "    copied = dict(updates)\n"
        "    if reason == 'auditor_result_validated' and context.state.repair_round == 1:\n"
        "        copied['summary'] = _transition\n"
        "    return _original_update(context, reason, copied)\n"
        "engine.build_initial_worker_prompt = _seed\n"
        "engine._update_state = _seed_update\n"
    )
    (injection_directory / "sitecustomize.py").write_text(
        injection_source,
        encoding="utf-8",
    )
    authoritative_environment = dict(os.environ)
    prior_pythonpath = authoritative_environment.get("PYTHONPATH")
    authoritative_environment["PYTHONPATH"] = (
        str(injection_directory)
        if not prior_pythonpath
        else f"{injection_directory}{os.pathsep}{prior_pythonpath}"
    )
    services = replace(
        services,
        authoritative_environ=authoritative_environment,
    )

    import research_automation_supervisor.shadow_sources as shadow_sources

    original_reconstruction = shadow_sources.build_initial_worker_prompt

    def reconstruct_seeded_prompt(
        prepared: Any,
        baseline: Any,
    ) -> Any:
        rendered = original_reconstruction(prepared, baseline)
        content = rendered.content + b"\n" + rendered_sentinel + b"\n"
        return replace(
            rendered,
            content=content,
            rendered_sha256=hashlib.sha256(content).hexdigest(),
            byte_count=len(content),
        )

    monkeypatch.setattr(
        shadow_sources,
        "build_initial_worker_prompt",
        reconstruct_seeded_prompt,
    )

    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    assert result.status == "awaiting_reviews"
    assert result.observed_decision_count == 4
    run_directory = Path(result.artifact_directory)
    authoritative_run = Path(str(result.authoritative_stage2_run))
    (project / values["future-sentinel"]).write_text(
        values["future-sentinel"],
        encoding="utf-8",
    )
    review_path = write_review(
        tmp_path / "domain-review.yaml",
        "worker_initial-r000-a001",
    )
    review_value = yaml.safe_load(review_path.read_text(encoding="utf-8"))
    review_value["notes"] = values["review"]
    review_path.write_text(
        yaml.safe_dump(review_value, sort_keys=False),
        encoding="utf-8",
    )
    record_live_shadow_review(
        run_directory,
        "worker_initial-r000-a001",
        review_path,
        services=services,
    )

    exact_stdin = base64.b64decode(
        _read_json(tmp_path / "earlier-supervisor-observation.json")[
            "prompt_base64"
        ]
    )
    exact_authoritative_prompt = base64.b64decode(
        _read_json(first_stage2_observation)["prompt_base64"]
    )
    assert rendered_sentinel in exact_authoritative_prompt
    later_stage2_entries = [
        json.loads(line)
        for line in (authoritative_run / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    later_state_transition = str(later_stage2_entries[-1]["entry_hash"])
    forbidden_exact_values = (
        human_source.rstrip(b"\n"),
        hashlib.sha256(exact_authoritative_prompt).hexdigest().encode("ascii"),
        *(value.encode("utf-8") for value in values.values()),
        later_state_transition.encode("ascii"),
    )
    for value in forbidden_exact_values:
        assert value
        assert value not in exact_stdin

    first_envelope = _read_json(
        run_directory / "decisions/worker_initial-r000-a001/envelope.json"
    )
    assert str(first_envelope["source_action_id"]).encode("ascii") in exact_stdin
    assert str(first_envelope["baseline_commit"]).encode("ascii") in exact_stdin
    assert b'"proposal_kind":"worker_initial"' in exact_stdin
    rendered_comparison = (
        run_directory
        / "comparisons/worker_initial-r000-a001/authoritative-rendered.md"
    ).read_bytes()
    assert rendered_sentinel in rendered_comparison
    for source in (
        human_source,
        (project / "control/contract.md").read_bytes(),
        (tmp_path / "live-control/project-context.md").read_bytes(),
    ):
        assert rendered_sentinel not in source

    transition_entries = [
        entry
        for entry in later_stage2_entries
        if transition_sentinel
        in json.dumps(entry, sort_keys=True).encode("utf-8")
    ]
    assert transition_entries
    assert all(
        entry["sequence"] > first_envelope["journal_intent_sequence"]
        for entry in transition_entries
    )
    for artifact in (authoritative_run / "audits").rglob("*"):
        if artifact.is_file():
            assert transition_sentinel not in artifact.read_bytes()
    first_decision = (
        run_directory / "decisions/worker_initial-r000-a001"
    )
    assert transition_sentinel not in (
        first_decision / "envelope.json"
    ).read_bytes()
    assert transition_sentinel not in (
        first_decision / "blind-input-manifest.json"
    ).read_bytes()
    for root in (
        first_decision,
        run_directory / "proposals/worker_initial-r000-a001",
        run_directory / "comparisons/worker_initial-r000-a001",
    ):
        for artifact in root.rglob("*"):
            if artifact.is_file():
                assert transition_sentinel not in artifact.read_bytes()


@pytest.mark.parametrize(
    ("scenario", "stage2_responses", "supervisor_kinds", "expected_ids"),
    (
        (
            "scope",
            [
                codex_response(
                    "worker",
                    SOURCE_WORKER_UUID,
                    worker_result(),
                    write_files={"outside.txt": "outside\n"},
                ),
                codex_response(
                    "worker",
                    SOURCE_WORKER_UUID,
                    worker_result(),
                    expected_resume_thread_id=SOURCE_WORKER_UUID,
                    delete_files=["outside.txt"],
                    write_files={"src/output.txt": "fixed\n"},
                ),
                codex_response(
                    "auditor",
                    SOURCE_AUDITOR_UUID,
                    auditor_result(),
                ),
            ],
            ("worker_initial", "worker_scope_repair", "auditor"),
            (
                "worker_initial-r000-a001",
                "worker_scope_repair-r001-a002",
                "auditor-r001-a003",
            ),
        ),
        (
            "test",
            [
                codex_response(
                    "worker",
                    SOURCE_WORKER_UUID,
                    worker_result(),
                ),
                codex_response(
                    "worker",
                    SOURCE_WORKER_UUID,
                    worker_result(),
                    expected_resume_thread_id=SOURCE_WORKER_UUID,
                    write_files={"src/ready.txt": "ready\n"},
                ),
                codex_response(
                    "auditor",
                    SOURCE_AUDITOR_UUID,
                    auditor_result(),
                ),
            ],
            ("worker_initial", "worker_test_repair", "auditor"),
            (
                "worker_initial-r000-a001",
                "worker_test_repair-r001-a002",
                "auditor-r001-a003",
            ),
        ),
        (
            "audit",
            [
                codex_response(
                    "worker",
                    SOURCE_WORKER_UUID,
                    worker_result(),
                ),
                codex_response(
                    "auditor",
                    SOURCE_AUDITOR_UUID,
                    auditor_result("fail_repairable"),
                ),
                codex_response(
                    "worker",
                    SOURCE_WORKER_UUID,
                    worker_result(),
                    expected_resume_thread_id=SOURCE_WORKER_UUID,
                    write_files={"src/output.txt": "repaired\n"},
                ),
                codex_response(
                    "auditor",
                    "33333333-3333-4333-8333-333333333333",
                    auditor_result(),
                ),
            ],
            (
                "worker_initial",
                "auditor",
                "worker_audit_repair",
                "auditor",
            ),
            (
                "worker_initial-r000-a001",
                "auditor-r000-a002",
                "worker_audit_repair-r001-a003",
                "auditor-r001-a004",
            ),
        ),
    ),
)
def test_live_observer_captures_repair_decision_kinds_exactly_once(
    tmp_path: Path,
    scenario: str,
    stage2_responses: list[dict[str, object]],
    supervisor_kinds: tuple[str, ...],
    expected_ids: tuple[str, ...],
) -> None:
    supervisor_responses = [
        live_supervisor_response(kind, resume=index > 0)
        for index, kind in enumerate(supervisor_kinds)
    ]
    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        stage2_responses=stage2_responses,
        supervisor_responses=supervisor_responses,
        test_requires_marker=scenario == "test",
    )
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    assert result.status == "awaiting_reviews"
    assert result.supervisor_session_id is not None
    run_directory = Path(result.artifact_directory)
    state = _read_json(run_directory / "state.json")
    assert tuple(state["observed_decision_ids"]) == expected_ids
    assert tuple(state["proposal_ids"]) == expected_ids
    assert tuple(state["comparison_ids"]) == expected_ids
    envelopes = [
        _read_json(
            run_directory / "decisions" / decision_id / "envelope.json"
        )
        for decision_id in expected_ids
    ]
    assert tuple(envelope["decision_id"] for envelope in envelopes) == expected_ids
    assert tuple(envelope["ordinal"] for envelope in envelopes) == tuple(
        range(1, len(expected_ids) + 1)
    )
    assert tuple(
        envelope["source_action_id"] for envelope in envelopes
    ) == tuple(
        (
            "auditor" if envelope["proposal_kind"] == "auditor" else "worker"
        )
        + f"-r{envelope['repair_round']:03d}"
        for envelope in envelopes
    )
    assert (
        tmp_path / "live-shadow-counter"
    ).read_text(encoding="ascii") == str(len(expected_ids))
    entries = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    assert sum(entry["event_type"] == "decision" for entry in entries) == len(
        expected_ids
    )
    assert sum(
        entry["event_type"] == "shadow_action_intent"
        for entry in entries
    ) == len(expected_ids)
    assert sum(
        entry["event_type"] == "shadow_action_completion"
        for entry in entries
    ) == len(expected_ids)


def test_live_observer_captures_externally_authorized_human_continuation(
    tmp_path: Path,
) -> None:
    instruction = tmp_path / "human-continuation.md"
    instruction_bytes = b"Create the externally authorized marker exactly.\n"
    instruction.write_bytes(instruction_bytes)
    stage2_responses = [
        codex_response(
            "worker",
            SOURCE_WORKER_UUID,
            worker_result(),
        ),
        codex_response(
            "worker",
            SOURCE_WORKER_UUID,
            worker_result(),
            expected_resume_thread_id=SOURCE_WORKER_UUID,
            write_files={"src/ready.txt": "ready\n"},
        ),
        codex_response(
            "auditor",
            SOURCE_AUDITOR_UUID,
            auditor_result(),
        ),
    ]
    supervisor_responses = [
        {
            **live_supervisor_response(
                "worker_initial",
                sleep_seconds=1.0,
            ),
            "observation_path": str(
                tmp_path / "initial-shadow-observation.json"
            ),
        },
        {
            **live_supervisor_response(
                "worker_human_continuation",
                resume=True,
            ),
            "observation_path": str(
                tmp_path / "continuation-shadow-observation.json"
            ),
        },
        live_supervisor_response("auditor", resume=True),
    ]
    spec, _, _, fake, services = create_live_shadow_tree(
        tmp_path,
        stage2_responses=stage2_responses,
        supervisor_responses=supervisor_responses,
        max_repair_rounds=0,
        test_requires_marker=True,
    )
    holder: dict[str, object] = {}

    def observe() -> None:
        holder["result"] = run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )

    observer = threading.Thread(target=observe, daemon=True)
    observer.start()
    deadline = time.monotonic() + 10
    authoritative_run: Path | None = None
    while time.monotonic() < deadline:
        candidates = tuple((tmp_path / "stage2-runs").glob("*"))
        if candidates:
            candidate = candidates[0]
            if (candidate / "result.json").is_file() and _read_json(
                candidate / "result.json"
            )["status"] == "repair_limit_paused":
                authoritative_run = candidate
                break
        time.sleep(0.005)
    assert authoritative_run is not None
    continued = None
    deadline = time.monotonic() + 2
    while continued is None and time.monotonic() < deadline:
        try:
            continued = continue_substage(
                authoritative_run,
                instruction,
                services=WorkflowServices(codex_executable=str(fake)),
            )
        except WorkflowLockError:
            time.sleep(0.005)
    assert continued is not None
    assert continued.status == "completed"
    observer.join(timeout=10)
    assert not observer.is_alive()
    result = holder["result"]
    assert hasattr(result, "status")
    assert result.status == "awaiting_reviews"  # type: ignore[union-attr]
    run_directory = Path(result.artifact_directory)  # type: ignore[union-attr]
    state = _read_json(run_directory / "state.json")
    expected_ids = (
        "worker_initial-r000-a001",
        "worker_human_continuation-r001-a002",
        "auditor-r001-a003",
    )
    assert tuple(state["observed_decision_ids"]) == expected_ids
    assert tuple(state["proposal_ids"]) == expected_ids
    assert tuple(state["comparison_ids"]) == expected_ids
    assert state["authoritative_status"] == "completed"
    continuation_observation = _read_json(
        tmp_path / "continuation-shadow-observation.json"
    )
    continuation_stdin = base64.b64decode(
        continuation_observation["prompt_base64"]
    )
    assert instruction_bytes not in continuation_stdin
    assert (
        tmp_path / "live-shadow-counter"
    ).read_text(encoding="ascii") == "3"


def test_shadow_delay_does_not_prevent_authoritative_completion(
    tmp_path: Path,
) -> None:
    from tests.live_shadow_helpers import live_supervisor_response

    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        supervisor_responses=[
            live_supervisor_response("worker_initial", sleep_seconds=0.2),
            live_supervisor_response("auditor", resume=True),
        ],
    )
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    assert result.authoritative_stage2_status == "completed"
    terminal = json.loads(
        (
            Path(result.artifact_directory) / "authoritative/result.json"
        ).read_text(encoding="utf-8")
    )
    first_supervisor = json.loads(
        (
            Path(result.artifact_directory)
            / "proposals/worker_initial-r000-a001/stage1-run/metadata.json"
        ).read_text(encoding="utf-8")
    )
    authoritative_result = json.loads(
        (
            Path(terminal["run_directory"]) / "result.json"
        ).read_text(encoding="utf-8")
    )
    assert authoritative_result["updated_at"] <= first_supervisor["ended_at"]


def test_live_reviews_are_immutable_and_readiness_stays_informational(
    tmp_path: Path,
) -> None:
    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    for index, proposal_id in enumerate(
        ("worker_initial-r000-a001", "auditor-r000-a002"),
        start=1,
    ):
        review = write_review(tmp_path / f"review-{index}.yaml", proposal_id)
        result = record_live_shadow_review(
            run_directory,
            proposal_id,
            review,
            services=services,
        )
    assert result.status == "completed"
    assert result.readiness == "candidate_ready_for_supervised_handoff"
    assert result.automation_enabled is False
    report = live_shadow_report(run_directory)
    assert report["readiness"]["informational_only"] is True
    assert report["readiness"]["automation_enabled"] is False
    assert all(
        assessment["review_status"] == "reviewed"
        for assessment in report["assessments"]
    )
    with pytest.raises(LiveShadowInputError, match="already"):
        record_live_shadow_review(
            run_directory,
            "worker_initial-r000-a001",
            tmp_path / "review-1.yaml",
            services=services,
        )


def test_supervisor_session_failure_is_isolated_from_authoritative_stage2(
    tmp_path: Path,
) -> None:
    from tests.live_shadow_helpers import live_supervisor_response
    from tests.shadow_helpers import SOURCE_WORKER_UUID

    response = live_supervisor_response("worker_initial")
    response["stdout_lines"] = [
        json.dumps({"type": "thread.started", "thread_id": SOURCE_WORKER_UUID})
    ]
    proposal = json.loads(str(response["final"]))
    proposal["prompt"] = "QUARANTINED-SHADOW-ONLY-SENTINEL"
    response["final"] = json.dumps(proposal, sort_keys=True)
    spec, _, project, _, services = create_live_shadow_tree(
        tmp_path,
        supervisor_responses=[response],
    )
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    assert result.status == "shadow_degraded"
    assert result.authoritative_stage2_status == "completed"
    assert result.shadow_failure_count >= 1
    authoritative_observation = json.loads(
        (project.parent / "fake-observation.json").read_text(encoding="utf-8")
    )
    authoritative_prompt = base64.b64decode(
        authoritative_observation["prompt_base64"]
    )
    assert b"QUARANTINED-SHADOW-ONLY-SENTINEL" not in authoritative_prompt
    authoritative_run = Path(str(result.authoritative_stage2_run))
    for artifact in authoritative_run.rglob("*"):
        if artifact.is_file():
            assert (
                b"QUARANTINED-SHADOW-ONLY-SENTINEL"
                not in artifact.read_bytes()
            ), artifact


def test_pre_thread_process_failure_is_typed_without_auth_contamination(
    tmp_path: Path,
) -> None:
    from tests.live_shadow_helpers import live_supervisor_response

    failed_start = live_supervisor_response("worker_initial")
    failed_start.update(
        {
            "exit_code": 1,
            "stdout_lines": [],
            "stderr": (
                "Not inside a trusted directory and "
                "--skip-git-repo-check was not specified."
            ),
            "write_final": False,
        }
    )
    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        supervisor_responses=[failed_start],
    )

    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )

    assert result.status == "shadow_degraded"
    assert result.authoritative_stage2_status == "completed"
    assert result.supervisor_session_id is None
    assert result.supervisor_session_usable is False
    assert result.auth_confidentiality_violation_detected is False
    assert (tmp_path / "live-shadow-counter").read_text(encoding="ascii") == "1"
    run_directory = Path(result.artifact_directory)
    state = json.loads(
        (run_directory / "state.json").read_text(encoding="utf-8")
    )
    reasons = {
        failure["reason"] for failure in state["shadow_failures"]
    }
    assert "supervisor_startup_transport_failure" in reasons
    assert "supervisor_session_unavailable" in reasons
    assert "auth_confidentiality_violation" not in reasons
    assert "runtime_home_contamination" not in reasons
    assert state["runtime_confidentiality_violation_intent_recorded"] is False
    assert state["runtime_home_cleanup_completed"] is False
    unlaunched_stage1 = (
        run_directory
        / "proposals/auditor-r000-a002/stage1-run"
    )
    assert not (unlaunched_stage1 / "metadata.json").exists()
    assert not (unlaunched_stage1 / "stage2-completion.json").exists()


@pytest.mark.parametrize(
    "unavailable_crash_point",
    (
        "after_unavailable_comparison_directory_creation",
        "after_unavailable_comparison_comparison_json",
        "after_unavailable_comparison_comparison_unavailable_json",
        "after_unavailable_comparison_directory_fsync",
        "after_unavailable_comparison_assessment_assessment_json",
        "before_unavailable_comparison_journal_append",
        "after_unavailable_comparison_journal_append",
    ),
)
def test_terminal_unfinished_authoritative_action_is_boundedly_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unavailable_crash_point: str,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, base_services = create_live_shadow_tree(tmp_path)
    authoritative_environment = dict(os.environ)
    # Stage 2 accepts the intent, then its ordinary adapter confidentiality
    # check fails on that action identity, leaving the action durably pending.
    authoritative_environment["CONTROLLED_BREAK_API_KEY"] = "worker-r000"
    clock = [datetime(2035, 1, 1, tzinfo=UTC)]
    services = replace(
        base_services,
        authoritative_environ=authoritative_environment,
        utc_now=lambda: clock[0],
        sleep=lambda _: time.sleep(0.005),
    )
    injected = False

    def stop_after_terminal_commit(point: str) -> None:
        nonlocal injected
        if injected or point != "after_state_replacement":
            return
        journals = tuple((tmp_path / "live-runs").glob("*/journal.jsonl"))
        if not journals:
            return
        lines = journals[0].read_text(encoding="ascii").splitlines()
        if not lines:
            return
        last = json.loads(lines[-1])
        if last["reason"] == "authoritative_stage2_terminal":
            injected = True
            raise RuntimeError("simulated collector crash after terminal commit")

    monkeypatch.setattr(
        engine,
        "_snapshot_checkpoint",
        stop_after_terminal_commit,
    )
    with pytest.raises(RuntimeError, match="terminal commit"):
        run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    assert injected
    run_directory = next((tmp_path / "live-runs").iterdir())
    temporary = live_shadow_status(run_directory)
    assert temporary.status == "authoritative_terminal_shadow_pending"
    assert temporary.authoritative_stage2_status == "human_paused"
    assert temporary.comparison_count == 0
    assert not tuple((run_directory / "comparisons").iterdir())

    # Let the already-launched shadow action finish; recovery may consume it,
    # but must never require an authoritative action completion that cannot exist.
    supervisor_completion = (
        run_directory
        / "proposals/worker_initial-r000-a001/stage1-run/stage2-completion.json"
    )
    deadline = time.monotonic() + 5
    while not supervisor_completion.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert supervisor_completion.is_file()
    authoritative_run = Path(str(temporary.authoritative_stage2_run))
    authoritative_before = {
        str(path.relative_to(authoritative_run)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in authoritative_run.rglob("*")
        if path.is_file()
    }

    unavailable_crashed = False

    def crash_unavailable(point: str) -> None:
        nonlocal unavailable_crashed
        if not unavailable_crashed and point == unavailable_crash_point:
            unavailable_crashed = True
            raise RuntimeError(
                f"simulated unavailable crash at {point}"
            )

    monkeypatch.setattr(engine, "_snapshot_checkpoint", crash_unavailable)
    clock[0] += timedelta(seconds=31)
    with pytest.raises(RuntimeError, match="unavailable crash"):
        resume_live_shadow(run_directory, services=services)
    assert unavailable_crashed
    monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
    recovered = resume_live_shadow(run_directory, services=services)
    assert recovered.status == "shadow_degraded"
    assert live_shadow_exit_code(recovered.status) == 5
    assert recovered.authoritative_stage2_status == "human_paused"
    assert recovered.authoritative_pause_reason == (
        "worker_adapter_input_or_dependency_failure"
    )
    assert recovered.observed_decision_count == 1
    assert recovered.proposal_count == 1
    assert recovered.comparison_count == 1
    comparison_directory = (
        run_directory / "comparisons/worker_initial-r000-a001"
    )
    comparison = _read_json(comparison_directory / "comparison.json")
    assert comparison["comparison_available"] is False
    assert comparison["comparison_unavailable_reason"] == (
        "authoritative_action_unfinished_after_terminal"
    )
    unavailable = _read_json(
        comparison_directory / "comparison-unavailable.json"
    )
    assert unavailable["source_action_id"] == "worker-r000"
    assert unavailable["authoritative_status"] == "human_paused"
    assert unavailable["reason"] == (
        "authoritative_action_unfinished_after_terminal"
    )
    assert not (comparison_directory / "authoritative-source.md").exists()
    assert not (comparison_directory / "authoritative-rendered.md").exists()
    report = live_shadow_report(run_directory)
    assert report["comparisons"][0]["comparison_unavailable_reason"] == (
        "authoritative_action_unfinished_after_terminal"
    )
    assert report["comparison_unavailable_records"] == [unavailable]
    assert any(
        failure["reason"]
        == "authoritative_action_unfinished_after_terminal"
        for failure in report["shadow_failures"]
    )
    assert live_shadow_status(run_directory) == recovered
    authoritative_after = {
        str(path.relative_to(authoritative_run)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in authoritative_run.rglob("*")
        if path.is_file()
    }
    assert authoritative_after == authoritative_before
    assert len(tuple((tmp_path / "stage2-runs").iterdir())) == 1


def test_resume_running_authority_consumes_original_supervisor_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        supervisor_responses=[
            live_supervisor_response(
                "worker_initial",
                sleep_seconds=0.4,
            ),
            live_supervisor_response("auditor", resume=True),
        ],
        stage2_responses=[
            codex_response(
                "worker",
                SOURCE_WORKER_UUID,
                worker_result(),
                sleep_seconds=0.15,
            ),
            codex_response(
                "auditor",
                SOURCE_AUDITOR_UUID,
                auditor_result(),
                sleep_seconds=0.6,
            ),
        ],
    )
    crashed = False

    def crash_during_original_supervisor(point: str) -> None:
        nonlocal crashed
        if crashed or point != "while_supervisor_in_flight":
            return
        runs = tuple((tmp_path / "live-runs").glob("*"))
        if not runs:
            return
        lines = (runs[0] / "journal.jsonl").read_text(
            encoding="ascii"
        ).splitlines()
        if not lines:
            return
        entries = [json.loads(line) for line in lines]
        state = _read_json(runs[0] / "state.json")
        if state["pending_action"] is not None and any(
            entry["event_type"] == "shadow_action_intent"
            for entry in entries
        ):
            crashed = True
            raise RuntimeError("simulated interrupted supervisor collector")

    monkeypatch.setattr(
        engine,
        "_snapshot_checkpoint",
        crash_during_original_supervisor,
    )
    with pytest.raises(RuntimeError, match="interrupted supervisor"):
        run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    assert crashed
    run_directory = next((tmp_path / "live-runs").iterdir())
    with pytest.raises(
        LiveShadowStateError,
        match="writer is still active",
    ):
        live_shadow_status(run_directory)
    interrupted_state = _read_json(run_directory / "state.json")
    assert interrupted_state["pending_action"]["proposal_id"] == (
        "worker_initial-r000-a001"
    )
    assert interrupted_state["authoritative_status"] is None

    monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
    recovered = resume_live_shadow(run_directory, services=services)
    assert recovered.status == "awaiting_reviews"
    assert recovered.authoritative_stage2_status == "completed"
    assert recovered.observed_decision_count == 2
    assert recovered.proposal_count == 2
    assert recovered.comparison_count == 2
    assert (tmp_path / "live-shadow-counter").read_text(encoding="ascii") == "2"
    assert (tmp_path / "stage2/fake-counter").read_text(encoding="ascii") == "2"
    assert len(tuple((tmp_path / "stage2-runs").iterdir())) == 1
    entries = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    assert sum(entry["reason"] == "authoritative_stage2_launched" for entry in entries) == 1
    for decision_id in (
        "worker_initial-r000-a001",
        "auditor-r000-a002",
    ):
        assert sum(
            entry["event_type"] == "decision"
            and entry["decision_id"] == decision_id
            for entry in entries
        ) == 1
        assert sum(
            entry["event_type"] == "shadow_action_intent"
            and entry["decision_id"] == decision_id
            for entry in entries
        ) == 1
        assert sum(
            entry["event_type"] == "shadow_action_completion"
            and entry["decision_id"] == decision_id
            for entry in entries
        ) == 1
        assert sum(
            entry["event_type"] == "comparison"
            and entry["decision_id"] == decision_id
            for entry in entries
        ) == 1


def test_interrupted_supervisor_deadline_finalizes_later_envelopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, base_services = create_live_shadow_tree(
        tmp_path,
        stage2_responses=[
            codex_response(
                "worker",
                SOURCE_WORKER_UUID,
                worker_result(),
                sleep_seconds=0.1,
            ),
            codex_response(
                "auditor",
                SOURCE_AUDITOR_UUID,
                auditor_result(),
                sleep_seconds=0.1,
            ),
        ],
    )
    release_original = threading.Event()
    supervisor_launches = [0]

    def never_complete(*args: object, **kwargs: object) -> object:
        del args, kwargs
        supervisor_launches[0] += 1
        release_original.wait()
        raise RuntimeError("original supervisor never completed")

    clock = [datetime(2036, 1, 1, tzinfo=UTC)]
    advanced_after_terminal = False

    def controlled_sleep(_: float) -> None:
        nonlocal advanced_after_terminal
        runs = tuple((tmp_path / "live-runs").glob("*/state.json"))
        if runs:
            state = _read_json(runs[0])
            if (
                state["status"]
                == "authoritative_terminal_shadow_pending"
                and not advanced_after_terminal
            ):
                clock[0] += timedelta(seconds=31)
                advanced_after_terminal = True
        time.sleep(0.005)

    services = replace(
        base_services,
        supervisor_invoker=never_complete,  # type: ignore[arg-type]
        utc_now=lambda: clock[0],
        sleep=controlled_sleep,
    )
    crashed = False

    def crash_with_pending_supervisor(point: str) -> None:
        nonlocal crashed
        if crashed or point != "while_supervisor_in_flight":
            return
        runs = tuple((tmp_path / "live-runs").glob("*"))
        if not runs:
            return
        state = _read_json(runs[0] / "state.json")
        lines = (runs[0] / "journal.jsonl").read_text(
            encoding="ascii"
        ).splitlines()
        if lines and state["pending_action"] is not None:
            crashed = True
            raise RuntimeError("simulated unresolved supervisor interruption")

    monkeypatch.setattr(
        engine,
        "_snapshot_checkpoint",
        crash_with_pending_supervisor,
    )
    try:
        with pytest.raises(RuntimeError, match="unresolved supervisor"):
            run_live_shadow(
                spec,
                runs_dir=tmp_path / "live-runs",
                stage2_runs_dir=tmp_path / "stage2-runs",
                services=services,
            )
        assert crashed
        run_directory = next((tmp_path / "live-runs").iterdir())
        release_original.set()
        time.sleep(0.02)
        monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
        recovered = resume_live_shadow(run_directory, services=services)
        assert recovered.status == "shadow_degraded"
        assert recovered.authoritative_stage2_status == "completed"
        assert recovered.observed_decision_count == 2
        assert recovered.proposal_count == 2
        assert recovered.comparison_count == 2
        assert recovered.shadow_failure_count >= 2
        assert supervisor_launches == [1]
        assert len(tuple((tmp_path / "stage2-runs").iterdir())) == 1
        state = _read_json(run_directory / "state.json")
        assert state["pending_action"] is None
        assert state["observed_decision_ids"] == [
            "worker_initial-r000-a001",
            "auditor-r000-a002",
        ]
        for decision_id in state["observed_decision_ids"]:
            failed = _read_json(
                run_directory
                / "proposals"
                / decision_id
                / "failed-supervisor-action.json"
            )
            assert failed["proposal_id"] == decision_id
            comparison = _read_json(
                run_directory
                / "comparisons"
                / decision_id
                / "comparison.json"
            )
            assert comparison["comparison_available"] is False
        entries = [
            json.loads(line)
            for line in (run_directory / "journal.jsonl")
            .read_text(encoding="ascii")
            .splitlines()
        ]
        assert sum(
            entry["reason"] == "authoritative_stage2_launched"
            for entry in entries
        ) == 1
        assert sum(
            entry["event_type"] == "shadow_action_intent"
            for entry in entries
        ) == 1
        assert sum(entry["event_type"] == "decision" for entry in entries) == 2
        assert sum(
            entry["event_type"] == "shadow_action_completion"
            for entry in entries
        ) == 2
        assert sum(
            entry["event_type"] == "comparison"
            for entry in entries
        ) == 2
        assert live_shadow_status(run_directory) == recovered
        report = live_shadow_report(run_directory)
        assert len(report["comparisons"]) == 2
        assert len(report["shadow_failures"]) >= 2
    finally:
        release_original.set()


def test_artifact_mutation_is_rejected_by_read_only_status(tmp_path: Path) -> None:
    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    candidate = (
        run_directory
        / "proposals/worker_initial-r000-a001/candidate-prompt.md"
    )
    candidate.write_text("replacement\n", encoding="utf-8")
    with pytest.raises(LiveShadowStateError, match="replaced evidence"):
        live_shadow_status(run_directory)


def test_abort_stops_only_observation_and_stage2_finishes(
    tmp_path: Path,
) -> None:
    from tests.shadow_helpers import SOURCE_AUDITOR_UUID, SOURCE_WORKER_UUID
    from tests.workflow_helpers import (
        auditor_result,
        codex_response,
        worker_result,
    )

    delayed_worker = codex_response(
        "worker",
        SOURCE_WORKER_UUID,
        worker_result(),
        sleep_seconds=0.6,
    )
    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        stage2_responses=[
            delayed_worker,
            codex_response(
                "auditor",
                SOURCE_AUDITOR_UUID,
                auditor_result(),
            ),
        ],
    )
    holder: dict[str, object] = {}

    def invoke() -> None:
        holder["result"] = run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )

    observer = threading.Thread(target=invoke)
    observer.start()
    run_directory: Path | None = None
    authoritative_run: Path | None = None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        candidates = tuple((tmp_path / "live-runs").glob("*"))
        if candidates and (candidates[0] / "state.json").is_file():
            state = json.loads(
                (candidates[0] / "state.json").read_text(encoding="utf-8")
            )
            if state["authoritative_run_directory"] is not None:
                run_directory = candidates[0]
                authoritative_run = Path(state["authoritative_run_directory"])
                break
        time.sleep(0.02)
    assert run_directory is not None
    assert authoritative_run is not None
    aborted = abort_live_shadow(
        run_directory,
        "operator stopped observation",
        services=services,
    )
    assert aborted.status == "aborted"
    observer.join(timeout=2)
    assert not observer.is_alive()
    assert holder["result"] == aborted

    deadline = time.monotonic() + 5
    authoritative_status = None
    while time.monotonic() < deadline:
        authoritative_status = json.loads(
            (authoritative_run / "result.json").read_text(encoding="utf-8")
        )["status"]
        if authoritative_status == "completed":
            break
        time.sleep(0.02)
    assert authoritative_status == "completed"
    assert substage_status(authoritative_run).status == "completed"
    assert live_shadow_status(run_directory).status == "aborted"


@pytest.mark.parametrize(
    "crash_point",
    (
        "after_journal_fsync",
        "after_review_journal_append",
        "before_result_replacement",
        "after_result_replacement",
        "before_state_replacement",
        "after_state_replacement",
    ),
)
def test_every_snapshot_midpoint_recovers_without_duplicate_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    first = write_review(
        tmp_path / "review-1.yaml",
        "worker_initial-r000-a001",
    )
    record_live_shadow_review(
        run_directory,
        "worker_initial-r000-a001",
        first,
        services=services,
    )
    second = write_review(tmp_path / "review-2.yaml", "auditor-r000-a002")
    state_before = (run_directory / "state.json").read_bytes()
    result_before = (run_directory / "result.json").read_bytes()
    authoritative_run = Path(str(result.authoritative_stage2_run))
    authoritative_hashes_before = {
        str(path.relative_to(authoritative_run)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in authoritative_run.rglob("*")
        if path.is_file()
    }
    crashed = False

    def inject(point: str) -> None:
        nonlocal crashed
        if not crashed and point == crash_point:
            crashed = True
            raise RuntimeError(f"simulated crash at {point}")

    monkeypatch.setattr(engine, "_snapshot_checkpoint", inject)
    with pytest.raises(RuntimeError, match="simulated crash"):
        record_live_shadow_review(
            run_directory,
            "auditor-r000-a002",
            second,
            services=services,
        )
    assert crashed
    state_after = (run_directory / "state.json").read_bytes()
    result_after = (run_directory / "result.json").read_bytes()
    if crash_point in {
        "after_journal_fsync",
        "after_review_journal_append",
        "before_result_replacement",
    }:
        assert state_after == state_before
        assert result_after == result_before
    elif crash_point in {
        "after_result_replacement",
        "before_state_replacement",
    }:
        assert state_after == state_before
        assert result_after != result_before
        assert json.loads(result_after)["review_count"] == 2
    else:
        assert state_after != state_before
        assert result_after != result_before
        assert json.loads(state_after)["reviewed_proposal_ids"] == [
            "worker_initial-r000-a001",
            "auditor-r000-a002",
        ]
        assert json.loads(result_after)["review_count"] == 2
    if crash_point == "after_state_replacement":
        assert live_shadow_status(run_directory).review_count == 2
    else:
        with pytest.raises(
            LiveShadowStateError,
            match="journal head",
        ):
            live_shadow_status(run_directory)

    monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
    with pytest.raises(LiveShadowInputError, match="already"):
        record_live_shadow_review(
            run_directory,
            "auditor-r000-a002",
            second,
            services=services,
        )
    recovered = live_shadow_status(run_directory)
    assert recovered.status == "completed"
    assert recovered.review_count == 2
    assert _read_json(run_directory / "state.json")["status"] == _read_json(
        run_directory / "result.json"
    )["status"]
    entries = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    assert sum(entry["reason"] == "authoritative_stage2_launched" for entry in entries) == 1
    assert sum(entry["event_type"] == "decision" for entry in entries) == 2
    assert sum(entry["event_type"] == "shadow_action_intent" for entry in entries) == 2
    assert sum(entry["event_type"] == "shadow_action_completion" for entry in entries) == 2
    assert sum(entry["event_type"] == "comparison" for entry in entries) == 2
    assert sum(entry["event_type"] == "review" for entry in entries) == 2
    state = _read_json(run_directory / "state.json")
    for key in (
        "observed_decision_ids",
        "proposal_ids",
        "comparison_ids",
        "reviewed_proposal_ids",
    ):
        values = state[key]
        assert len(values) == len(set(values))
    authoritative_hashes_after = {
        str(path.relative_to(authoritative_run)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in authoritative_run.rglob("*")
        if path.is_file()
    }
    assert authoritative_hashes_after == authoritative_hashes_before


@pytest.mark.parametrize(
    "crash_point",
    (
        "after_decision_directory_creation",
        "after_decision_envelope_json",
        "after_decision_envelope_sha256",
        "after_decision_blind_input_manifest_json",
        "after_decision_output_schema_json",
        "after_decision_directory_fsync",
        "before_decision_journal_append",
        "after_decision_journal_append",
    ),
)
def test_decision_prejournal_artifact_boundaries_recover_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    crashed = False

    def inject(point: str) -> None:
        nonlocal crashed
        if not crashed and point == crash_point:
            crashed = True
            raise RuntimeError(f"simulated crash at {point}")

    monkeypatch.setattr(engine, "_snapshot_checkpoint", inject)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    assert crashed
    run_directory = next((tmp_path / "live-runs").iterdir())
    decision_directory = (
        run_directory / "decisions/worker_initial-r000-a001"
    )
    before = {
        path.name: (
            path.read_bytes(),
            path.stat().st_ino,
            path.stat().st_mtime_ns,
        )
        for path in decision_directory.iterdir()
        if path.is_file()
    }
    monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
    recovered = resume_live_shadow(
        run_directory,
        services=services,
    )
    assert recovered.status == "awaiting_reviews"
    assert set(path.name for path in decision_directory.iterdir()) == {
        "envelope.json",
        "envelope.sha256",
        "blind-input-manifest.json",
        "output-schema.json",
    }
    for name, (content, inode, mtime_ns) in before.items():
        path = decision_directory / name
        assert path.read_bytes() == content
        assert path.stat().st_ino == inode
        assert path.stat().st_mtime_ns == mtime_ns
    envelope = _read_json(decision_directory / "envelope.json")
    assert (
        decision_directory / "envelope.sha256"
    ).read_text(encoding="ascii") == f"{envelope['envelope_sha256']}\n"
    entries = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    assert sum(entry["event_type"] == "decision" for entry in entries) == 2
    assert sum(
        entry["decision_id"] == "worker_initial-r000-a001"
        and entry["event_type"] == "decision"
        for entry in entries
    ) == 1
    assert sum(
        entry["reason"] == "authoritative_stage2_launched"
        for entry in entries
    ) == 1
    assert sum(
        entry["event_type"] == "shadow_action_intent"
        for entry in entries
    ) == 2
    assert sum(
        entry["event_type"] == "shadow_action_completion"
        for entry in entries
    ) == 2
    assert sum(entry["event_type"] == "comparison" for entry in entries) == 2
    assert (
        tmp_path / "live-shadow-counter"
    ).read_text(encoding="ascii") == "2"


@pytest.mark.parametrize(
    "mutation",
    (
        "contradictory_file",
        "extra_file",
        "symlink",
        "nonregular",
        "envelope_hash_mismatch",
    ),
)
def test_decision_prejournal_contradictions_remain_integrity_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)

    def crash(point: str) -> None:
        if point == "after_decision_directory_fsync":
            raise RuntimeError("decision prepared")

    monkeypatch.setattr(engine, "_snapshot_checkpoint", crash)
    with pytest.raises(RuntimeError, match="decision prepared"):
        run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    run_directory = next((tmp_path / "live-runs").iterdir())
    directory = run_directory / "decisions/worker_initial-r000-a001"
    if mutation == "contradictory_file":
        (directory / "output-schema.json").write_bytes(b"{}\n")
    elif mutation == "extra_file":
        (directory / "extra.txt").write_text("unexpected", encoding="utf-8")
    elif mutation == "symlink":
        (directory / "envelope.json").unlink()
        (directory / "envelope.json").symlink_to(
            directory / "output-schema.json"
        )
    elif mutation == "nonregular":
        (directory / "envelope.json").unlink()
        os.mkfifo(directory / "envelope.json")
    else:
        (directory / "envelope.sha256").write_text(
            f"{'f' * 64}\n",
            encoding="ascii",
        )
    monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
    with pytest.raises(
        LiveShadowStateError,
        match="pre-journal|artifact",
    ):
        resume_live_shadow(run_directory, services=services)


@pytest.mark.parametrize(
    "artifact_domain",
    (
        "decision",
        "proposal",
        "comparison",
        "comparison_assessment",
        "unavailable_comparison",
        "unavailable_comparison_assessment",
    ),
)
@pytest.mark.parametrize("failed_fsync_call", (1, 2, 3, 4))
def test_required_artifact_directory_fsync_failures_recover_without_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_domain: str,
    failed_fsync_call: int,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    root = tmp_path / artifact_domain
    destination = root / "artifact-set"
    journal = root / "journal.jsonl"
    root.mkdir()
    journal.write_bytes(b"semantic-prefix\n")
    authoritative = tmp_path / "authoritative-stage2"
    authoritative.mkdir()
    authoritative_marker = authoritative / "unchanged"
    authoritative_marker.write_bytes(b"authoritative")
    expected = {
        "one.json": b'{"value":1}\n',
        "two.txt": b"two\n",
    }
    original_fsync = engine._fsync_directory
    calls = 0

    def fail_selected(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == failed_fsync_call:
            raise OSError("injected directory fsync failure")
        original_fsync(path)

    monkeypatch.setattr(engine, "_fsync_directory", fail_selected)
    with pytest.raises(
        (LiveShadowIntegrityError, LiveShadowStateError),
    ) as captured:
        engine._prepare_artifact_set(
            destination,
            expected,
            checkpoint_prefix=artifact_domain,
        )
    assert "injected directory fsync failure" not in str(captured.value)
    assert journal.read_bytes() == b"semantic-prefix\n"
    assert authoritative_marker.read_bytes() == b"authoritative"
    matching_before = {
        path.name: (
            path.read_bytes(),
            path.stat().st_ino,
            path.stat().st_mtime_ns,
        )
        for path in destination.iterdir()
        if path.is_file() and path.name in expected
    }

    monkeypatch.setattr(engine, "_fsync_directory", original_fsync)
    engine._prepare_artifact_set(
        destination,
        expected,
        checkpoint_prefix=artifact_domain,
    )
    assert journal.read_bytes() == b"semantic-prefix\n"
    for name, (content, inode, mtime_ns) in matching_before.items():
        path = destination / name
        assert path.read_bytes() == content
        assert path.stat().st_ino == inode
        assert path.stat().st_mtime_ns == mtime_ns
    assert {
        path.name: path.read_bytes()
        for path in destination.iterdir()
        if path.is_file()
    } == expected

    (destination / "one.json").write_bytes(b"contradiction\n")
    with pytest.raises(
        LiveShadowIntegrityError,
        match="contradicts",
    ):
        engine._prepare_artifact_set(
            destination,
            expected,
            checkpoint_prefix=artifact_domain,
        )
    assert journal.read_bytes() == b"semantic-prefix\n"
    assert authoritative_marker.read_bytes() == b"authoritative"


@pytest.mark.parametrize(
    "failure_point",
    ("open", "fsync", "close", "fsync_and_close"),
)
def test_required_directory_fsync_helper_never_suppresses_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    original_open = engine.os.open
    original_fsync = engine.os.fsync
    original_close = engine.os.close

    if failure_point == "open":
        monkeypatch.setattr(
            engine.os,
            "open",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("open failure")
            ),
        )
    if failure_point in {"fsync", "fsync_and_close"}:
        monkeypatch.setattr(
            engine.os,
            "fsync",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("primary fsync failure")
            ),
        )
    if failure_point in {"close", "fsync_and_close"}:
        def fail_close(descriptor: int) -> None:
            original_close(descriptor)
            raise OSError("close failure")

        monkeypatch.setattr(engine.os, "close", fail_close)
    try:
        with pytest.raises(OSError) as captured:
            engine._fsync_directory(tmp_path)
        if failure_point == "fsync_and_close":
            assert str(captured.value) == "primary fsync failure"
    finally:
        monkeypatch.setattr(engine.os, "open", original_open)
        monkeypatch.setattr(engine.os, "fsync", original_fsync)
        monkeypatch.setattr(engine.os, "close", original_close)


@pytest.mark.parametrize("snapshot_name", ("result", "state"))
def test_snapshot_directory_fsync_failure_replays_one_durable_semantic_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_name: str,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    review = write_review(
        tmp_path / "review.yaml",
        "worker_initial-r000-a001",
    )
    original_fsync = engine._fsync_directory
    root_fsync_calls = 0
    target_call = 1 if snapshot_name == "result" else 2

    def fail_snapshot(path: Path) -> None:
        nonlocal root_fsync_calls
        if path == run_directory:
            root_fsync_calls += 1
            if root_fsync_calls == target_call:
                raise OSError("injected snapshot directory fsync failure")
        original_fsync(path)

    monkeypatch.setattr(engine, "_fsync_directory", fail_snapshot)
    with pytest.raises(LiveShadowStateError) as captured:
        record_live_shadow_review(
            run_directory,
            "worker_initial-r000-a001",
            review,
            services=services,
        )
    assert "injected snapshot" not in str(captured.value)
    review_artifact = (
        run_directory / "reviews/worker_initial-r000-a001.json"
    )
    before = (
        review_artifact.read_bytes(),
        review_artifact.stat().st_ino,
        review_artifact.stat().st_mtime_ns,
    )
    monkeypatch.setattr(engine, "_fsync_directory", original_fsync)
    with pytest.raises(LiveShadowInputError, match="already"):
        record_live_shadow_review(
            run_directory,
            "worker_initial-r000-a001",
            review,
            services=services,
        )
    assert (
        review_artifact.read_bytes(),
        review_artifact.stat().st_ino,
        review_artifact.stat().st_mtime_ns,
    ) == before
    entries = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    assert sum(
        entry["event_type"] == "review"
        and entry["decision_id"] == "worker_initial-r000-a001"
        for entry in entries
    ) == 1
    recovered = live_shadow_status(run_directory, services=services)
    assert recovered.review_count == 1
    assert _read_json(run_directory / "state.json")["status"] == _read_json(
        run_directory / "result.json"
    )["status"]


@pytest.mark.parametrize(
    "crash_point",
    (
        "after_proposal_supervisor_result_json",
        "after_proposal_candidate_prompt_md",
        "after_proposal_supervisor_action_json",
        "after_proposal_directory_fsync",
        "before_proposal_journal_append",
        "after_proposal_journal_append",
        "after_authoritative_reconstruction",
        "after_comparison_authoritative_source_md",
        "after_comparison_authoritative_rendered_md",
        "after_comparison_comparison_json",
        "after_comparison_assessment_assessment_json",
        "before_comparison_journal_append",
        "after_comparison_journal_append",
    ),
)
def test_proposal_and_comparison_prejournal_boundaries_recover_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    crashed = False

    def inject(point: str) -> None:
        nonlocal crashed
        if not crashed and point == crash_point:
            crashed = True
            raise RuntimeError(f"simulated crash at {point}")

    monkeypatch.setattr(engine, "_snapshot_checkpoint", inject)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    assert crashed
    run_directory = next((tmp_path / "live-runs").iterdir())
    authoritative_run_record = _read_json(
        run_directory / "authoritative/stage2-run.json"
    )
    authoritative_run = Path(authoritative_run_record["run_directory"])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (
            authoritative_run / "result.json"
        ).is_file() and _read_json(
            authoritative_run / "result.json"
        )["status"] == "completed":
            break
        time.sleep(0.01)
    assert _read_json(authoritative_run / "result.json")["status"] == "completed"
    authoritative_before = {
        str(path.relative_to(authoritative_run)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in authoritative_run.rglob("*")
        if path.is_file()
    }
    partial_files = {
        path: (
            path.read_bytes(),
            path.stat().st_ino,
            path.stat().st_mtime_ns,
        )
        for root in (
            run_directory / "proposals/worker_initial-r000-a001",
            run_directory / "comparisons/worker_initial-r000-a001",
        )
        if root.exists()
        for path in root.iterdir()
        if path.is_file()
    }
    monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
    recovered = resume_live_shadow(run_directory, services=services)
    assert recovered.status == "awaiting_reviews"
    for path, (content, inode, mtime_ns) in partial_files.items():
        assert path.read_bytes() == content
        assert path.stat().st_ino == inode
        assert path.stat().st_mtime_ns == mtime_ns
    entries = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    assert sum(
        entry["reason"] == "authoritative_stage2_launched"
        for entry in entries
    ) == 1
    assert sum(entry["event_type"] == "decision" for entry in entries) == 2
    assert sum(
        entry["event_type"] == "shadow_action_intent"
        for entry in entries
    ) == 2
    assert sum(
        entry["event_type"] == "shadow_action_completion"
        for entry in entries
    ) == 2
    assert sum(entry["event_type"] == "comparison" for entry in entries) == 2
    assert (
        tmp_path / "live-shadow-counter"
    ).read_text(encoding="ascii") == "2"
    authoritative_after = {
        str(path.relative_to(authoritative_run)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in authoritative_run.rglob("*")
        if path.is_file()
    }
    assert authoritative_after == authoritative_before


@pytest.mark.parametrize(
    ("crash_point", "expected_status", "expected_stage2_runs"),
    (
        (
            "after_authoritative_launch_intent_before_child_launch",
            "human_paused",
            0,
        ),
        (
            "after_authoritative_child_launch_before_identity",
            "human_paused",
            1,
        ),
        (
            "after_authoritative_child_identity_before_journal",
            "awaiting_reviews",
            1,
        ),
        (
            "after_authoritative_discovery_before_journal",
            "awaiting_reviews",
            1,
        ),
    ),
)
def test_authoritative_launch_and_discovery_crash_boundaries_never_relaunch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
    expected_status: str,
    expected_stage2_runs: int,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    crashed = False

    def inject(point: str) -> None:
        nonlocal crashed
        if not crashed and point == crash_point:
            crashed = True
            raise RuntimeError(f"simulated crash at {point}")

    monkeypatch.setattr(engine, "_snapshot_checkpoint", inject)
    with pytest.raises(RuntimeError, match="simulated crash"):
        run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    assert crashed
    run_directory = next((tmp_path / "live-runs").iterdir())
    if expected_stage2_runs:
        deadline = time.monotonic() + 10
        while (
            not tuple((tmp_path / "stage2-runs").glob("*/state.json"))
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
    monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
    recovered = resume_live_shadow(run_directory, services=services)
    assert recovered.status == expected_status
    stage2_runs = tuple(
        path.parent
        for path in (tmp_path / "stage2-runs").glob("*/state.json")
    )
    assert len(stage2_runs) == expected_stage2_runs
    if expected_stage2_runs:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (
                stage2_runs[0] / "result.json"
            ).is_file() and _read_json(
                stage2_runs[0] / "result.json"
            )["status"] == "completed":
                break
            time.sleep(0.01)
        assert _read_json(stage2_runs[0] / "result.json")["status"] == "completed"
    entries = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    assert sum(
        entry["reason"] in {
            "authoritative_stage2_launched",
            "authoritative_stage2_launch_recovered",
        }
        for entry in entries
    ) <= 1
    if expected_status == "awaiting_reviews":
        assert sum(
            entry["reason"] == "authoritative_run_discovered"
            for entry in entries
        ) == 1


def test_journal_ahead_recovery_rejects_a_contradictory_result_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    review = write_review(
        tmp_path / "review.yaml",
        "worker_initial-r000-a001",
    )

    def crash(point: str) -> None:
        if point == "after_journal_fsync":
            raise RuntimeError("journal durable")

    monkeypatch.setattr(engine, "_snapshot_checkpoint", crash)
    with pytest.raises(RuntimeError, match="journal durable"):
        record_live_shadow_review(
            run_directory,
            "worker_initial-r000-a001",
            review,
            services=services,
        )
    contradictory = _read_json(run_directory / "result.json")
    contradictory["summary"] = "externally contradictory snapshot"
    (run_directory / "result.json").write_text(
        json.dumps(contradictory, indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
    with pytest.raises(
        LiveShadowStateError,
        match="contradicts every recoverable journal generation",
    ):
        record_live_shadow_review(
            run_directory,
            "worker_initial-r000-a001",
            review,
            services=services,
        )
