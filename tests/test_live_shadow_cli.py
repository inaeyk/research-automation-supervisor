from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import quote

import pytest
from typer.testing import CliRunner

from research_automation_supervisor.cli import app
from research_automation_supervisor.errors import (
    LiveShadowDependencyError,
    LiveShadowIntegrityError,
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
from tests.live_shadow_helpers import create_live_shadow_tree
from tests.shadow_helpers import write_review
from tests.workflow_helpers import auditor_result, codex_response, worker_result

runner = CliRunner()


def test_validate_live_shadow_cli_is_read_only(tmp_path: Path) -> None:
    spec, _, _, _, _ = create_live_shadow_tree(tmp_path)
    before = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    result = runner.invoke(app, ["validate-live-shadow-spec", str(spec), "--json"])
    human = runner.invoke(app, ["validate-live-shadow-spec", str(spec)])
    after = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    assert result.exit_code == 0
    assert '"live_shadow_id": "minimal-live-shadow"' in result.stdout
    assert human.exit_code == 0
    assert "Valid live shadow minimal-live-shadow" in human.stdout
    assert before == after


def test_all_stage4_commands_are_registered() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "validate-live-shadow-spec",
        "run-live-shadow",
        "resume-live-shadow",
        "live-shadow-status",
        "record-live-shadow-review",
        "live-shadow-report",
        "abort-live-shadow",
    ):
        assert command in result.stdout


def test_live_shadow_exit_codes_are_frozen() -> None:
    assert live_shadow_exit_code("completed") == 0
    assert live_shadow_exit_code("awaiting_reviews") == 5
    assert live_shadow_exit_code("shadow_degraded") == 5
    assert live_shadow_exit_code("human_paused") == 5
    assert live_shadow_exit_code("failed") == 4
    assert live_shadow_exit_code("aborted") == 8


def test_run_live_shadow_bubblewrap_dependency_error_exits_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.cli as cli

    spec, _, _, _, _ = create_live_shadow_tree(tmp_path)

    def missing_backend(*_: object, **__: object) -> object:
        raise LiveShadowDependencyError(
            "Bubblewrap synthetic-root capability probe failed"
        )

    monkeypatch.setattr(cli, "run_live_shadow", missing_backend)
    result = runner.invoke(
        app,
        ["run-live-shadow", str(spec), "--json"],
    )
    assert result.exit_code == 3
    assert '"ok": false' in result.stdout
    assert "Bubblewrap" in result.stdout


def test_all_stage4_cli_commands_execute_real_boundaries_and_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.cli as cli

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    monkeypatch.setattr(
        cli,
        "run_live_shadow",
        lambda path, *, runs_dir, stage2_runs_dir: run_live_shadow(
            path,
            runs_dir=runs_dir,
            stage2_runs_dir=stage2_runs_dir,
            services=services,
        ),
    )
    run_result = runner.invoke(
        app,
        [
            "run-live-shadow",
            str(spec),
            "--runs-dir",
            str(tmp_path / "live-runs"),
            "--stage2-runs-dir",
            str(tmp_path / "stage2-runs"),
            "--json",
        ],
    )
    assert run_result.exit_code == 5
    run_payload = json.loads(run_result.stdout)
    assert run_payload["status"] == "awaiting_reviews"
    run_directory = Path(run_payload["artifact_directory"])
    assert len(tuple((tmp_path / "stage2-runs").iterdir())) == 1

    monkeypatch.setattr(
        cli,
        "live_shadow_status",
        lambda path: live_shadow_status(path, services=services),
    )
    monkeypatch.setattr(
        cli,
        "live_shadow_report",
        lambda path: live_shadow_report(path, services=services),
    )
    before = {
        path: (path.stat().st_mtime_ns, path.read_bytes())
        for path in run_directory.rglob("*")
        if path.is_file()
    }
    status_result = runner.invoke(
        app,
        ["live-shadow-status", str(run_directory), "--json"],
    )
    assert status_result.exit_code == 0
    assert json.loads(status_result.stdout)["status"] == "awaiting_reviews"
    human_status = runner.invoke(
        app,
        ["live-shadow-status", str(run_directory)],
    )
    assert human_status.exit_code == 0
    assert "Status: awaiting_reviews" in human_status.stdout
    report_result = runner.invoke(
        app,
        ["live-shadow-report", str(run_directory)],
    )
    assert report_result.exit_code == 0
    assert "Readiness is informational only" in report_result.stdout
    json_report = runner.invoke(
        app,
        ["live-shadow-report", str(run_directory), "--json"],
    )
    assert json_report.exit_code == 0
    assert json.loads(json_report.stdout)["status"] == "awaiting_reviews"
    after = {
        path: (path.stat().st_mtime_ns, path.read_bytes())
        for path in run_directory.rglob("*")
        if path.is_file()
    }
    assert after == before

    monkeypatch.setattr(
        cli,
        "record_live_shadow_review",
        lambda path, proposal_id, review_path: record_live_shadow_review(
            path,
            proposal_id,
            review_path,
            services=services,
        ),
    )
    first_review = write_review(
        tmp_path / "review-1.yaml",
        "worker_initial-r000-a001",
    )
    first_result = runner.invoke(
        app,
        [
            "record-live-shadow-review",
            str(run_directory),
            "worker_initial-r000-a001",
            str(first_review),
            "--json",
        ],
    )
    assert first_result.exit_code == 5
    assert json.loads(first_result.stdout)["review_count"] == 1
    duplicate = runner.invoke(
        app,
        [
            "record-live-shadow-review",
            str(run_directory),
            "worker_initial-r000-a001",
            str(first_review),
            "--json",
        ],
    )
    assert duplicate.exit_code == 2
    assert json.loads(duplicate.stdout)["ok"] is False
    second_review = write_review(
        tmp_path / "review-2.yaml",
        "auditor-r000-a002",
    )
    completed = runner.invoke(
        app,
        [
            "record-live-shadow-review",
            str(run_directory),
            "auditor-r000-a002",
            str(second_review),
        ],
    )
    assert completed.exit_code == 0
    assert "Status: completed" in completed.stdout
    completed_json = runner.invoke(
        app,
        ["live-shadow-status", str(run_directory), "--json"],
    )
    assert completed_json.exit_code == 0
    assert json.loads(completed_json.stdout)["status"] == "completed"

    envelope = (
        run_directory
        / "decisions/worker_initial-r000-a001/envelope.json"
    )
    envelope.write_bytes(envelope.read_bytes() + b" ")
    integrity = runner.invoke(
        app,
        ["live-shadow-status", str(run_directory), "--json"],
    )
    assert integrity.exit_code == 4
    assert json.loads(integrity.stdout)["ok"] is False


@pytest.mark.parametrize("json_mode", (False, True))
def test_abort_cli_leaves_authoritative_stage2_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    json_mode: bool,
) -> None:
    import research_automation_supervisor.cli as cli

    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        stage2_responses=[
            codex_response(
                "worker",
                "11111111-1111-4111-8111-111111111111",
                worker_result(),
                sleep_seconds=1.0,
            ),
            codex_response(
                "auditor",
                "22222222-2222-4222-8222-222222222222",
                auditor_result(),
            ),
        ],
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
    run_directory: Path | None = None
    while time.monotonic() < deadline:
        candidates = tuple((tmp_path / "live-runs").glob("*"))
        if candidates and (
            candidates[0] / "authoritative/launch.json"
        ).is_file():
            run_directory = candidates[0]
            break
        time.sleep(0.01)
    assert run_directory is not None
    monkeypatch.setattr(
        cli,
        "abort_live_shadow",
        lambda path, reason: abort_live_shadow(
            path,
            reason,
            services=services,
        ),
    )
    command = [
            "abort-live-shadow",
            str(run_directory),
            "--reason",
            "operator stop",
        ]
    if json_mode:
        command.append("--json")
    aborted = runner.invoke(app, command)
    assert aborted.exit_code == 8
    if json_mode:
        assert json.loads(aborted.stdout)["status"] == "aborted"
    else:
        assert "Status: aborted" in aborted.stdout
    observer.join(timeout=5)
    assert not observer.is_alive()
    deadline = time.monotonic() + 10
    while (
        not tuple((tmp_path / "stage2-runs").glob("*"))
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    stage2_run = next((tmp_path / "stage2-runs").iterdir())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if (stage2_run / "result.json").is_file() and json.loads(
            (stage2_run / "result.json").read_text(encoding="utf-8")
        )["status"] == "completed":
            break
        time.sleep(0.01)
    assert json.loads(
        (stage2_run / "result.json").read_text(encoding="utf-8")
    )["status"] == "completed"


@pytest.mark.parametrize("json_mode", (False, True))
def test_resume_cli_recovers_without_duplicate_authoritative_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    json_mode: bool,
) -> None:
    import research_automation_supervisor.cli as cli
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    crashed = False

    def inject(point: str) -> None:
        nonlocal crashed
        if not crashed and point == "after_decision_envelope_json":
            crashed = True
            raise RuntimeError("simulated observer crash")

    monkeypatch.setattr(engine, "_snapshot_checkpoint", inject)
    with pytest.raises(RuntimeError, match="observer crash"):
        run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    run_directory = next((tmp_path / "live-runs").iterdir())
    monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
    monkeypatch.setattr(
        cli,
        "resume_live_shadow",
        lambda path: resume_live_shadow(path, services=services),
    )
    command = ["resume-live-shadow", str(run_directory)]
    if json_mode:
        command.append("--json")
    resumed = runner.invoke(app, command)
    assert resumed.exit_code == 5
    if json_mode:
        assert json.loads(resumed.stdout)["status"] == "awaiting_reviews"
    else:
        assert "Status: awaiting_reviews" in resumed.stdout
    assert len(tuple((tmp_path / "stage2-runs").iterdir())) == 1
    journal = (
        run_directory / "journal.jsonl"
    ).read_text(encoding="ascii").splitlines()
    assert sum(
        json.loads(line)["reason"] == "authoritative_stage2_launched"
        for line in journal
    ) == 1


def test_run_live_shadow_human_mode_executes_one_authoritative_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.cli as cli

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    monkeypatch.setattr(
        cli,
        "run_live_shadow",
        lambda path, *, runs_dir, stage2_runs_dir: run_live_shadow(
            path,
            runs_dir=runs_dir,
            stage2_runs_dir=stage2_runs_dir,
            services=services,
        ),
    )
    result = runner.invoke(
        app,
        [
            "run-live-shadow",
            str(spec),
            "--runs-dir",
            str(tmp_path / "live-runs"),
            "--stage2-runs-dir",
            str(tmp_path / "stage2-runs"),
        ],
    )
    assert result.exit_code == 5
    assert "Status: awaiting_reviews" in result.stdout
    assert len(tuple((tmp_path / "stage2-runs").iterdir())) == 1


@pytest.mark.parametrize(
    "command",
    (
        ("run-live-shadow", "spec.yaml"),
        ("resume-live-shadow", "run"),
        ("live-shadow-status", "run"),
        (
            "record-live-shadow-review",
            "run",
            "worker_initial-r000-a001",
            "review.yaml",
        ),
        ("live-shadow-report", "run"),
        ("abort-live-shadow", "run", "--reason", "operator stop"),
    ),
)
@pytest.mark.parametrize("json_mode", (False, True))
def test_every_stage4_cli_failure_redacts_current_authentication_forms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: tuple[str, ...],
    json_mode: bool,
) -> None:
    import research_automation_supervisor.cli as cli

    unique = hashlib.sha256(os.urandom(32)).hexdigest()
    secret = f'AUTH-{unique}/+\\"END'
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    raw = secret.encode()
    protected = (
        raw,
        json.dumps(secret)[1:-1].encode(),
        base64.b64encode(raw),
        base64.urlsafe_b64encode(raw),
        raw.hex().encode("ascii"),
        quote(secret, safe="").encode("ascii"),
        f"Bearer {secret}".encode(),
    )
    exception_text = "adapter failure " + " ".join(
        fragment.decode("ascii", errors="ignore")
        for fragment in protected
    )

    def protected_failure(*_: object, **__: object) -> object:
        raise LiveShadowIntegrityError(exception_text)

    target = {
        "run-live-shadow": "run_live_shadow",
        "resume-live-shadow": "resume_live_shadow",
        "live-shadow-status": "live_shadow_status",
        "record-live-shadow-review": "record_live_shadow_review",
        "live-shadow-report": "live_shadow_report",
        "abort-live-shadow": "abort_live_shadow",
    }[command[0]]
    monkeypatch.setattr(cli, target, protected_failure)
    arguments = list(command)
    if json_mode:
        arguments.append("--json")
    result = runner.invoke(app, arguments)
    assert result.exit_code == 4
    captured = (result.stdout + result.stderr).encode()
    for fragment in protected:
        assert fragment not in captured
    if json_mode:
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
    else:
        assert "Live-shadow error:" in result.stderr


@pytest.mark.parametrize(
    ("command_name", "json_mode"),
    (
        ("live-shadow-status", False),
        ("live-shadow-status", True),
        ("live-shadow-report", False),
        ("live-shadow-report", True),
    ),
)
def test_status_and_report_cli_reject_contaminated_runtime_names_nonsecretly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
    json_mode: bool,
) -> None:
    import research_automation_supervisor.cli as cli

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    auth = tmp_path / "fake-auth.json"
    services = replace(services, codex_authentication_file=auth)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    authoritative = Path(str(result.authoritative_stage2_run))
    authoritative_before = {
        path.relative_to(authoritative): path.read_bytes()
        for path in authoritative.rglob("*")
        if path.is_file()
    }
    secret = f"CLI-RUNTIME-{hashlib.sha256(os.urandom(32)).hexdigest()}"
    auth.write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    codex_home = tmp_path / "cli-codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_bytes(auth.read_bytes())
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime_home = run_directory / "quarantine" / "codex-home"
    (runtime_home / f"Bearer {secret}").write_text(secret, encoding="utf-8")
    operation = (
        live_shadow_status
        if command_name == "live-shadow-status"
        else live_shadow_report
    )
    monkeypatch.setattr(
        cli,
        (
            "live_shadow_status"
            if command_name == "live-shadow-status"
            else "live_shadow_report"
        ),
        lambda path: operation(path, services=services),
    )
    arguments = [command_name, str(run_directory)]
    if json_mode:
        arguments.append("--json")
    captured = runner.invoke(app, arguments)
    assert captured.exit_code == 4
    output = (captured.stdout + captured.stderr).encode()
    for fragment in (
        secret.encode(),
        base64.b64encode(secret.encode()),
        base64.urlsafe_b64encode(secret.encode()),
        secret.encode().hex().encode("ascii"),
        quote(secret, safe="").encode("ascii"),
        f"Bearer {secret}".encode(),
    ):
        assert fragment not in output
    if json_mode:
        assert json.loads(captured.stdout)["ok"] is False
    else:
        assert "Live-shadow error:" in captured.stderr
    assert (runtime_home / f"Bearer {secret}").exists()
    authoritative_after = {
        path.relative_to(authoritative): path.read_bytes()
        for path in authoritative.rglob("*")
        if path.is_file()
    }
    assert authoritative_after == authoritative_before


@pytest.mark.parametrize("json_mode", (False, True))
def test_review_cli_contamination_invalidates_without_recording_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    json_mode: bool,
) -> None:
    import research_automation_supervisor.cli as cli

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    auth = tmp_path / "fake-auth.json"
    services = replace(services, codex_authentication_file=auth)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    secret = f"CLI-REVIEW-{hashlib.sha256(os.urandom(32)).hexdigest()}"
    auth.write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    codex_home = tmp_path / "cli-review-codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_bytes(auth.read_bytes())
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime_home = run_directory / "quarantine" / "codex-home"
    contaminated = runtime_home / secret.encode("utf-8").hex()
    contaminated.write_text(secret, encoding="utf-8")
    review = write_review(
        tmp_path / "review.yaml",
        "worker_initial-r000-a001",
    )
    monkeypatch.setattr(
        cli,
        "record_live_shadow_review",
        lambda path, proposal_id, review_path: record_live_shadow_review(
            path,
            proposal_id,
            review_path,
            services=services,
        ),
    )
    command = [
        "record-live-shadow-review",
        str(run_directory),
        "worker_initial-r000-a001",
        str(review),
    ]
    if json_mode:
        command.append("--json")
    captured = runner.invoke(app, command)
    assert captured.exit_code == 4
    if json_mode:
        assert json.loads(captured.stdout)["ok"] is False
    else:
        assert "Live-shadow error:" in captured.stderr
    output = (captured.stdout + captured.stderr).encode()
    assert secret.encode("utf-8") not in output
    assert secret.encode("utf-8").hex().encode("ascii") not in output
    assert not (
        run_directory / "reviews/worker_initial-r000-a001.json"
    ).exists()
    state = json.loads(
        (run_directory / "state.json").read_text(encoding="utf-8")
    )
    assert state["supervisor_session_usable"] is False
    assert state["runtime_home_cleanup_completed"] is True
    assert not contaminated.exists()


def test_status_and_report_cli_do_not_heal_pending_cleanup_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.cli as cli
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    auth = tmp_path / "fake-auth.json"
    services = replace(services, codex_authentication_file=auth)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    secret = f"CLI-PENDING-{hashlib.sha256(os.urandom(32)).hexdigest()}"
    auth.write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    codex_home = tmp_path / "cli-pending-codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_bytes(auth.read_bytes())
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime_home = run_directory / "quarantine" / "codex-home"
    (runtime_home / secret.encode("utf-8").hex()).write_text(
        secret,
        encoding="utf-8",
    )
    review = write_review(
        tmp_path / "pending-review.yaml",
        "worker_initial-r000-a001",
    )

    def crash(point: str) -> None:
        if point == "before_runtime_home_scrub":
            raise RuntimeError("crash before scrub")

    monkeypatch.setattr(engine, "_snapshot_checkpoint", crash)
    with pytest.raises(RuntimeError, match="before scrub"):
        record_live_shadow_review(
            run_directory,
            "worker_initial-r000-a001",
            review,
            services=services,
        )
    monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
    monkeypatch.setattr(
        cli,
        "live_shadow_status",
        lambda path: live_shadow_status(path, services=services),
    )
    monkeypatch.setattr(
        cli,
        "live_shadow_report",
        lambda path: live_shadow_report(path, services=services),
    )
    journal_before = (run_directory / "journal.jsonl").read_bytes()
    state_before = (run_directory / "state.json").read_bytes()
    result_before = (run_directory / "result.json").read_bytes()
    for command_name in ("live-shadow-status", "live-shadow-report"):
        for json_mode in (False, True):
            command = [command_name, str(run_directory)]
            if json_mode:
                command.append("--json")
            captured = runner.invoke(app, command)
            assert captured.exit_code == 4
            if json_mode:
                assert json.loads(captured.stdout)["ok"] is False
            else:
                assert "Live-shadow error:" in captured.stderr
            output = (captured.stdout + captured.stderr).encode()
            assert secret.encode("utf-8") not in output
            assert secret.encode("utf-8").hex().encode("ascii") not in output
    assert (run_directory / "journal.jsonl").read_bytes() == journal_before
    assert (run_directory / "state.json").read_bytes() == state_before
    assert (run_directory / "result.json").read_bytes() == result_before


@pytest.mark.parametrize("cleanup_failure", (False, True))
@pytest.mark.parametrize("json_mode", (False, True))
def test_abort_cli_scrubs_authentication_path_or_fails_integrity_nonsecretly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: bool,
    json_mode: bool,
) -> None:
    import research_automation_supervisor.cli as cli
    import research_automation_supervisor.live_shadow_isolation as isolation

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    auth = tmp_path / "fake-auth.json"
    services = replace(services, codex_authentication_file=auth)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    authoritative = Path(str(result.authoritative_stage2_run))
    authoritative_before = {
        path.relative_to(authoritative): path.read_bytes()
        for path in authoritative.rglob("*")
        if path.is_file()
    }
    secret = f"CLI-ABORT-{hashlib.sha256(os.urandom(32)).hexdigest()}"
    auth.write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    codex_home = tmp_path / "cli-codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_bytes(auth.read_bytes())
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    runtime_home = run_directory / "quarantine" / "codex-home"
    contaminated = runtime_home / secret.encode().hex()
    contaminated.write_text(secret, encoding="utf-8")
    journal_before = (run_directory / "journal.jsonl").read_bytes()
    if cleanup_failure:
        monkeypatch.setattr(
            isolation,
            "_remove_runtime_entry_at",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("cleanup failed with " + secret)
            ),
        )
    monkeypatch.setattr(
        cli,
        "abort_live_shadow",
        lambda path, reason: abort_live_shadow(
            path,
            reason,
            services=services,
        ),
    )
    command = [
        "abort-live-shadow",
        str(run_directory),
        "--reason",
        "operator stop",
    ]
    if json_mode:
        command.append("--json")
    captured = runner.invoke(app, command)
    assert captured.exit_code == (4 if cleanup_failure else 8)
    if json_mode:
        payload = json.loads(captured.stdout)
        if cleanup_failure:
            assert payload["ok"] is False
        else:
            assert payload["status"] == "aborted"
    elif cleanup_failure:
        assert "Live-shadow error:" in captured.stderr
    else:
        assert "Status: aborted" in captured.stdout
    output = (captured.stdout + captured.stderr).encode()
    for fragment in (
        secret.encode(),
        base64.b64encode(secret.encode()),
        base64.urlsafe_b64encode(secret.encode()),
        secret.encode().hex().encode("ascii"),
        quote(secret, safe="").encode("ascii"),
        f"Bearer {secret}".encode(),
    ):
        assert fragment not in output
    if cleanup_failure:
        journal_after = (run_directory / "journal.jsonl").read_bytes()
        assert journal_after.startswith(journal_before)
        entries = [
            json.loads(line)
            for line in journal_after.decode("ascii").splitlines()
        ]
        assert sum(
            entry["event_type"]
            == "runtime_confidentiality_violation_intent"
            for entry in entries
        ) == 1
        state = json.loads(
            (run_directory / "state.json").read_text(encoding="utf-8")
        )
        assert state["supervisor_session_usable"] is False
        assert state["runtime_home_cleanup_required"] is True
    else:
        assert not contaminated.exists()
    authoritative_after = {
        path.relative_to(authoritative): path.read_bytes()
        for path in authoritative.rglob("*")
        if path.is_file()
    }
    assert authoritative_after == authoritative_before


@pytest.mark.parametrize("json_mode", (False, True))
def test_status_cli_authentication_parser_failure_is_generic_and_parseable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    json_mode: bool,
) -> None:
    import research_automation_supervisor.cli as cli

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    auth = tmp_path / "fake-auth.json"
    services = replace(services, codex_authentication_file=auth)
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    secret = f"INVALID-AUTH-{hashlib.sha256(os.urandom(32)).hexdigest()}"
    auth.write_text(
        '{"access_token":"' + secret,
        encoding="utf-8",
    )
    codex_home = tmp_path / "cli-codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_bytes(auth.read_bytes())
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(
        cli,
        "live_shadow_status",
        lambda path: live_shadow_status(path, services=services),
    )
    arguments = ["live-shadow-status", str(run_directory)]
    if json_mode:
        arguments.append("--json")
    captured = runner.invoke(app, arguments)
    assert captured.exit_code == 3
    output = (captured.stdout + captured.stderr).encode()
    assert secret.encode() not in output
    if json_mode:
        assert json.loads(captured.stdout)["ok"] is False
    else:
        assert "Live-shadow error:" in captured.stderr
