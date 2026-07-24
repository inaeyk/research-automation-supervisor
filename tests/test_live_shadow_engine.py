from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from research_automation_supervisor.errors import (
    LiveShadowInputError,
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
    assert live_shadow_status(run_directory) == result
    report = live_shadow_report(run_directory)
    assert report["automation_enabled"] is False


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
        if crashed or point != "after_state_replacement":
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
        if (
            state["pending_action"] is not None
            and any(
                entry["event_type"] == "shadow_action_intent"
                for entry in entries
            )
            and entries[-1]["reason"]
            == "authoritative_journal_entry_observed"
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
    interrupted = live_shadow_status(run_directory)
    assert interrupted.status == "authoritative_running"
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
        if crashed or point != "after_state_replacement":
            return
        runs = tuple((tmp_path / "live-runs").glob("*"))
        if not runs:
            return
        state = _read_json(runs[0] / "state.json")
        lines = (runs[0] / "journal.jsonl").read_text(
            encoding="ascii"
        ).splitlines()
        if (
            lines
            and state["pending_action"] is not None
            and json.loads(lines[-1])["reason"]
            == "authoritative_journal_entry_observed"
        ):
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
