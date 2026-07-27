from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import stat
import subprocess
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import pytest

from research_automation_supervisor.auth_confidentiality import (
    load_authentication_confidentiality,
)
from research_automation_supervisor.codex_adapter import (
    CodexProcessLaunch,
    build_codex_command,
)
from research_automation_supervisor.codex_models import (
    ROLE_POLICIES,
    CodexRunRequest,
    PreparedCodexRequest,
)
from research_automation_supervisor.errors import (
    LiveShadowDependencyError,
    LiveShadowInputError,
    LiveShadowIntegrityError,
    LiveShadowLockError,
    LiveShadowStateError,
)
from research_automation_supervisor.live_shadow_engine import (
    LiveShadowServices,
    abort_live_shadow,
    live_shadow_exit_code,
    live_shadow_report,
    live_shadow_status,
    record_live_shadow_review,
    resume_live_shadow,
    run_live_shadow,
)
from research_automation_supervisor.live_shadow_isolation import (
    ISOLATED_ACTION_DIRECTORY,
    ISOLATED_CODEX_PATH,
    ISOLATED_HOME,
    ISOLATED_OUTPUT_SCHEMA_PATH,
    ISOLATED_TMPDIR,
    ISOLATED_WORKSPACE_PATH,
    BubblewrapBackendIdentity,
    BubblewrapCapability,
    _Mount,
    _RuntimeHomeConsistencyError,
    _RuntimeHomeDisappearanceError,
    _scan_runtime_home_once,
    _validate_mount_allowlist,
    build_bubblewrap_process_launch,
    inspect_runtime_home_contents,
    preflight_bubblewrap_isolation,
    scrub_runtime_home_contamination,
    validate_runtime_home_contents,
)
from research_automation_supervisor.live_shadow_models import LiveShadowResult
from tests.live_shadow_helpers import (
    create_live_shadow_tree,
    live_supervisor_response,
)
from tests.shadow_helpers import (
    SOURCE_AUDITOR_UUID,
    SOURCE_WORKER_UUID,
    SUPERVISOR_UUID,
    supervisor_proposal,
    write_review,
)
from tests.workflow_helpers import (
    auditor_result,
    codex_response,
    git,
    worker_result,
)


def test_authentication_fragments_cover_sensitive_encodings_without_repr(
    tmp_path: Path,
) -> None:
    access = "ACCESS-unique-0123456789/+"
    refresh = "REFRESH-unique-9876543210_-"
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiJ1bmlxdWUtdXNlciJ9."
        "uniquesignaturevalue"
    )
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps(
            {
                "mode": "subscription",
                "provider": "openai",
                "expires_at": "2099-01-01",
                "tokens": {
                    "access_token": access,
                    "refresh_token": refresh,
                },
                "identity": jwt,
            }
        ),
        encoding="utf-8",
    )
    protection = load_authentication_confidentiality(
        auth,
        forbidden_roots=(tmp_path / "forbidden", tmp_path / "runs"),
    )
    fragments = protection.text_fragments()
    assert protection.enabled is True
    assert protection.scan_completed is True
    assert protection.protected_logical_value_count == 4
    for secret in (access, refresh, jwt):
        raw = secret.encode("utf-8")
        assert secret in fragments
        assert base64.b64encode(raw).decode("ascii") in fragments
        assert base64.urlsafe_b64encode(raw).decode("ascii") in fragments
        assert raw.hex() in fragments
        assert quote(secret, safe="") in fragments
        assert f"Bearer {secret}" in fragments
        assert protection.contains_bytes(raw)
    rendered = repr(protection)
    assert access not in rendered
    assert refresh not in rendered
    assert jwt not in rendered


@pytest.mark.parametrize(
    "kind",
    ("symlink", "nonregular", "malformed", "forbidden", "unsafe_field"),
)
def test_authentication_derivation_fails_closed_safely(
    tmp_path: Path,
    kind: str,
) -> None:
    target = tmp_path / "target-auth.json"
    target.write_text(
        json.dumps({"access_token": "unique-secret-0123456789"}),
        encoding="utf-8",
    )
    auth = target
    forbidden = tmp_path / "forbidden"
    forbidden.mkdir()
    if kind == "symlink":
        auth = tmp_path / "auth.json"
        auth.symlink_to(target)
    elif kind == "nonregular":
        auth = tmp_path / "auth-dir"
        auth.mkdir()
    elif kind == "malformed":
        auth.write_text("{not-json", encoding="utf-8")
    elif kind == "forbidden":
        auth = forbidden / "auth.json"
        auth.write_text(
            json.dumps({"access_token": "unique-secret-0123456789"}),
            encoding="utf-8",
        )
    elif kind == "unsafe_field":
        auth.write_text(
            json.dumps({"access_token": {"value": 123}}),
            encoding="utf-8",
        )
    with pytest.raises(
        LiveShadowDependencyError,
        match="authentication is unavailable",
    ) as captured:
        load_authentication_confidentiality(
            auth,
            forbidden_roots=(forbidden, tmp_path / "runs"),
        )
    assert "unique-secret" not in str(captured.value)


def _raw_runtime_tree(root: Path) -> bytes:
    chunks: list[bytes] = []
    for current, directories, files in os.walk(os.fsencode(root)):
        relative = os.path.relpath(current, os.fsencode(root))
        chunks.append(relative)
        chunks.extend(directories)
        chunks.extend(files)
        for name in files:
            path = os.path.join(current, name)
            try:
                chunks.append(Path(os.fsdecode(path)).read_bytes())
            except OSError:
                continue
    return b"\0".join(chunks)


def _completed_live_shadow_run(
    tmp_path: Path,
) -> tuple[Path, Path, LiveShadowServices, LiveShadowResult]:
    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    authentication_file = tmp_path / "fake-auth.json"
    services = replace(
        services,
        codex_authentication_file=authentication_file,
    )
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
        result = record_live_shadow_review(
            run_directory,
            proposal_id,
            write_review(
                tmp_path / f"completed-review-{index}.yaml",
                proposal_id,
            ),
            services=services,
        )
    assert result.status == "completed"
    assert result.authoritative_stage2_status == "completed"
    assert result.review_count == 2
    return run_directory, authentication_file, services, result


def test_runtime_home_scans_raw_path_components_and_complete_relative_paths(
    tmp_path: Path,
) -> None:
    unique = hashlib.sha256(os.urandom(32)).hexdigest()
    secret = f'AUTH-{unique}\\"END'
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    protection = load_authentication_confidentiality(
        auth,
        forbidden_roots=(tmp_path / "repository", tmp_path / "stage2-runs"),
    )
    raw = secret.encode("utf-8")
    protected = (
        raw,
        json.dumps(secret)[1:-1].encode("utf-8"),
        base64.b64encode(raw),
        base64.urlsafe_b64encode(raw),
        raw.hex().encode("ascii"),
        quote(secret, safe="").encode("ascii"),
        f"Bearer {secret}".encode(),
    )
    runtime_home = tmp_path / "runtime-home"
    runtime_home.mkdir()
    runtime_bytes = os.fsencode(runtime_home)
    for index, fragment in enumerate(protected):
        target = os.path.join(
            runtime_bytes,
            b"nested",
            str(index).encode("ascii"),
            fragment,
        )
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if index % 2:
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, b"safe"), "wb") as handle:
                handle.write(b"safe")
        else:
            with open(target, "wb") as handle:
                handle.write(b"safe")
    non_utf8 = os.path.join(
        runtime_bytes,
        b"raw-\xff-" + raw.hex().encode("ascii"),
    )
    with open(non_utf8, "wb") as handle:
        handle.write(b"safe")

    with pytest.raises(
        LiveShadowIntegrityError,
        match="clean-content invariant",
    ) as captured:
        validate_runtime_home_contents(
            runtime_home,
            authentication_confidentiality=protection,
        )
    assert secret not in str(captured.value)
    findings = scrub_runtime_home_contamination(
        runtime_home,
        authentication_confidentiality=protection,
    )
    assert findings == ("auth_confidentiality_violation",)
    raw_tree = _raw_runtime_tree(runtime_home)
    for fragment in protected:
        assert fragment not in raw_tree
    assert raw.hex().encode("ascii") not in raw_tree


@pytest.mark.parametrize(
    "entry_kind",
    (
        "symlink",
        "fifo",
        "socket",
        "device",
        "hard_link",
        "git_directory",
        "repository_marker",
        "forbidden_locator",
    ),
)
def test_runtime_home_rejects_and_scrubs_recursive_boundary_entries(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    runtime_home = tmp_path / "runtime-home"
    runtime_home.mkdir()
    authentication = tmp_path / "auth.json"
    authentication.write_text(
        json.dumps({"access_token": "unique-runtime-auth-0123456789"}),
        encoding="utf-8",
    )
    protection = load_authentication_confidentiality(
        authentication,
        forbidden_roots=(tmp_path / "repo", tmp_path / "runs"),
    )
    forbidden = b"/fixed/authoritative/stage2/run"
    open_socket: socket.socket | None = None
    if entry_kind == "symlink":
        (runtime_home / "entry").symlink_to(authentication)
    elif entry_kind == "fifo":
        os.mkfifo(runtime_home / "entry")
    elif entry_kind == "socket":
        open_socket = socket.socket(socket.AF_UNIX)
        try:
            open_socket.bind(str(runtime_home / "entry"))
        except OSError:
            open_socket.close()
            open_socket = None
            os.mknod(
                runtime_home / "entry",
                stat.S_IFSOCK | stat.S_IRUSR | stat.S_IWUSR,
            )
    elif entry_kind == "device":
        try:
            os.mknod(
                runtime_home / "entry",
                stat.S_IFCHR | stat.S_IRUSR | stat.S_IWUSR,
                os.makedev(1, 3),
            )
        except PermissionError:
            pytest.skip("creating a device entry requires unavailable permission")
    elif entry_kind == "hard_link":
        source = tmp_path / "hard-link-source"
        source.write_bytes(b"safe")
        os.link(source, runtime_home / "entry")
    elif entry_kind == "git_directory":
        (runtime_home / ".git").mkdir()
    elif entry_kind == "repository_marker":
        (runtime_home / "entry").write_bytes(
            b"repositoryformatversion = 0\n"
        )
    else:
        (runtime_home / "entry").write_bytes(forbidden)
    try:
        with pytest.raises(
            LiveShadowIntegrityError,
            match="clean-content invariant",
        ):
            validate_runtime_home_contents(
                runtime_home,
                authentication_confidentiality=protection,
                forbidden_fragments=(forbidden,),
            )
        findings = scrub_runtime_home_contamination(
            runtime_home,
            authentication_confidentiality=protection,
            forbidden_fragments=(forbidden,),
        )
        assert findings
        validate_runtime_home_contents(
            runtime_home,
            authentication_confidentiality=protection,
            forbidden_fragments=(forbidden,),
        )
    finally:
        if open_socket is not None:
            open_socket.close()


@pytest.mark.parametrize(
    ("bound_name", "value"),
    (
        ("MAX_RUNTIME_HOME_DEPTH", 1),
        ("MAX_RUNTIME_HOME_FILES", 2),
        ("MAX_RUNTIME_HOME_FILE_BYTES", 3),
        ("MAX_RUNTIME_HOME_BYTES", 3),
    ),
)
def test_runtime_home_enforces_every_recursive_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound_name: str,
    value: int,
) -> None:
    import research_automation_supervisor.live_shadow_isolation as isolation

    runtime_home = tmp_path / "runtime-home"
    runtime_home.mkdir()
    if bound_name == "MAX_RUNTIME_HOME_DEPTH":
        (runtime_home / "one" / "two").mkdir(parents=True)
    elif bound_name == "MAX_RUNTIME_HOME_FILES":
        for index in range(value + 1):
            (runtime_home / f"entry-{index}").write_bytes(b"x")
    elif bound_name == "MAX_RUNTIME_HOME_FILE_BYTES":
        (runtime_home / "entry").write_bytes(b"x" * (value + 1))
    else:
        (runtime_home / "one").write_bytes(b"xx")
        (runtime_home / "two").write_bytes(b"xx")
    monkeypatch.setattr(isolation, bound_name, value)
    with pytest.raises(
        LiveShadowIntegrityError,
        match="clean-content invariant",
    ):
        validate_runtime_home_contents(runtime_home)
    findings = scrub_runtime_home_contamination(
        runtime_home,
        authentication_confidentiality=(
            load_authentication_confidentiality(
                _write_test_authentication(tmp_path),
                forbidden_roots=(tmp_path / "repo", tmp_path / "runs"),
            )
        ),
    )
    assert "runtime_home_bound_violation" in findings
    assert not tuple(runtime_home.iterdir())


def _write_test_authentication(tmp_path: Path) -> Path:
    path = tmp_path / "boundary-auth.json"
    path.write_text(
        json.dumps({"access_token": "boundary-auth-0123456789"}),
        encoding="utf-8",
    )
    return path


def test_runtime_home_retries_disappearance_but_rejects_identity_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.live_shadow_isolation as isolation

    runtime_home = tmp_path / "runtime-home"
    runtime_home.mkdir()
    (runtime_home / "entry").write_bytes(b"safe")
    calls = 0

    def disappear_once(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            (runtime_home / "entry").unlink()
            raise _RuntimeHomeDisappearanceError
        _scan_runtime_home_once(*args, **kwargs)

    monkeypatch.setattr(
        isolation,
        "_scan_runtime_home_once",
        disappear_once,
    )
    validate_runtime_home_contents(runtime_home)
    assert calls == 2

    replacement = runtime_home / "replacement"
    replacement.write_bytes(b"safe")

    def changed_identity(*_: object, **__: object) -> None:
        raise _RuntimeHomeConsistencyError

    monkeypatch.setattr(
        isolation,
        "_scan_runtime_home_once",
        changed_identity,
    )
    with pytest.raises(
        LiveShadowIntegrityError,
        match="identity changed",
    ):
        validate_runtime_home_contents(runtime_home)


@pytest.mark.parametrize("repetition", range(5))
def test_runtime_root_actual_replacement_never_accepts_detached_clean_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repetition: int,
) -> None:
    import research_automation_supervisor.live_shadow_isolation as isolation

    secret = (
        f"ROOT-REPLACEMENT-{repetition}-"
        f"{hashlib.sha256(os.urandom(32)).hexdigest()}"
    )
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    protection = load_authentication_confidentiality(
        auth,
        forbidden_roots=(tmp_path / "repo", tmp_path / "stage2-runs"),
    )
    runtime_home = tmp_path / "runtime-home"
    runtime_home.mkdir()
    (runtime_home / "clean").write_bytes(b"clean")
    detached = tmp_path / f"detached-root-{repetition}"
    replaced = False

    def replace_root(point: str) -> None:
        nonlocal replaced
        if point != "runtime_root_opened" or replaced:
            return
        replaced = True
        runtime_home.rename(detached)
        runtime_home.mkdir()
        (runtime_home / "current").write_text(secret, encoding="utf-8")

    monkeypatch.setattr(
        isolation,
        "_runtime_scan_checkpoint",
        replace_root,
    )
    findings = inspect_runtime_home_contents(
        runtime_home,
        authentication_confidentiality=protection,
    )
    assert replaced is True
    assert findings == ("auth_confidentiality_violation",)
    with pytest.raises(
        LiveShadowIntegrityError,
        match="clean-content invariant",
    ) as captured:
        validate_runtime_home_contents(
            runtime_home,
            authentication_confidentiality=protection,
        )
    assert secret not in str(captured.value)
    assert detached.name not in str(captured.value)


@pytest.mark.parametrize("repetition", range(5))
def test_runtime_regular_entry_actual_replacement_is_rechecked_by_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    repetition: int,
) -> None:
    import research_automation_supervisor.live_shadow_isolation as isolation

    secret = (
        f"FILE-REPLACEMENT-{repetition}-"
        f"{hashlib.sha256(os.urandom(32)).hexdigest()}"
    )
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    protection = load_authentication_confidentiality(
        auth,
        forbidden_roots=(tmp_path / "repo", tmp_path / "stage2-runs"),
    )
    runtime_home = tmp_path / "runtime-home"
    runtime_home.mkdir()
    current = runtime_home / "current"
    current.write_bytes(b"clean")
    replacement = tmp_path / "replacement"
    replacement.write_text(secret, encoding="utf-8")
    replaced = False

    def replace_regular_entry(point: str) -> None:
        nonlocal replaced
        if point != "runtime_regular_file_read" or replaced:
            return
        replaced = True
        os.replace(replacement, current)

    monkeypatch.setattr(
        isolation,
        "_runtime_scan_checkpoint",
        replace_regular_entry,
    )
    findings = inspect_runtime_home_contents(
        runtime_home,
        authentication_confidentiality=protection,
    )
    assert replaced is True
    assert findings == ("auth_confidentiality_violation",)
    assert current.read_text(encoding="utf-8") == secret


def test_runtime_directory_entry_actual_replacement_is_rechecked_by_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.live_shadow_isolation as isolation

    secret = (
        "DIRECTORY-REPLACEMENT-"
        f"{hashlib.sha256(os.urandom(32)).hexdigest()}"
    )
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    protection = load_authentication_confidentiality(
        auth,
        forbidden_roots=(tmp_path / "repo", tmp_path / "stage2-runs"),
    )
    runtime_home = tmp_path / "runtime-home"
    runtime_home.mkdir()
    current = runtime_home / "current"
    current.mkdir()
    (current / "clean").write_bytes(b"clean")
    detached = runtime_home / "detached"
    replaced = False

    def replace_directory_entry(point: str) -> None:
        nonlocal replaced
        if point != "runtime_directory_opened" or replaced:
            return
        replaced = True
        current.rename(detached)
        current.mkdir()
        (current / "current").write_text(secret, encoding="utf-8")

    monkeypatch.setattr(
        isolation,
        "_runtime_scan_checkpoint",
        replace_directory_entry,
    )
    findings = inspect_runtime_home_contents(
        runtime_home,
        authentication_confidentiality=protection,
    )
    assert replaced is True
    assert findings == ("auth_confidentiality_violation",)


def test_runtime_concurrent_replacement_stress_detects_or_fails_closed(
    tmp_path: Path,
) -> None:
    secret = (
        "WRITER-STRESS-"
        f"{hashlib.sha256(os.urandom(32)).hexdigest()}"
    )
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    protection = load_authentication_confidentiality(
        auth,
        forbidden_roots=(tmp_path / "repo", tmp_path / "stage2-runs"),
    )
    runtime_home = tmp_path / "runtime-home"
    runtime_home.mkdir()
    current = runtime_home / "current"
    current.write_bytes(b"clean")
    stop = threading.Event()
    started = threading.Event()

    def write_replacements() -> None:
        index = 0
        while not stop.is_set():
            replacement = runtime_home / f"temporary-{index % 2}"
            replacement.write_bytes(secret.encode("utf-8"))
            os.replace(replacement, current)
            started.set()
            index += 1

    writer = threading.Thread(target=write_replacements, daemon=True)
    writer.start()
    assert started.wait(timeout=5)
    try:
        try:
            findings = inspect_runtime_home_contents(
                runtime_home,
                authentication_confidentiality=protection,
            )
        except LiveShadowIntegrityError as exc:
            assert "stable" in str(exc) or "identity changed" in str(exc)
        else:
            assert findings == ("auth_confidentiality_violation",)
    finally:
        stop.set()
        writer.join(timeout=5)
    assert not writer.is_alive()


def test_review_root_replacement_durably_invalidates_before_full_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.live_shadow_isolation as isolation

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    services = replace(
        services,
        codex_authentication_file=tmp_path / "fake-auth.json",
    )
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
    secret = (
        "REVIEW-ROOT-RACE-"
        f"{hashlib.sha256(os.urandom(32)).hexdigest()}"
    )
    (tmp_path / "fake-auth.json").write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    runtime_home = run_directory / "quarantine" / "codex-home"
    detached = run_directory / "quarantine" / "detached-runtime-root"
    replacement_name = secret.encode("utf-8").hex()
    replaced = False

    def replace_root(point: str) -> None:
        nonlocal replaced
        if point != "runtime_root_opened" or replaced:
            return
        replaced = True
        runtime_home.rename(detached)
        runtime_home.mkdir(mode=0o700)
        (runtime_home / replacement_name).write_text(
            secret,
            encoding="utf-8",
        )

    monkeypatch.setattr(
        isolation,
        "_runtime_scan_checkpoint",
        replace_root,
    )
    review = write_review(
        tmp_path / "review.yaml",
        "worker_initial-r000-a001",
    )
    with pytest.raises(
        LiveShadowIntegrityError,
        match="confidentiality boundary",
    ) as captured:
        record_live_shadow_review(
            run_directory,
            "worker_initial-r000-a001",
            review,
            services=services,
        )
    assert replaced is True
    assert secret not in str(captured.value)
    assert replacement_name not in str(captured.value)
    state = json.loads(
        (run_directory / "state.json").read_text(encoding="utf-8")
    )
    assert state["supervisor_session_usable"] is False
    assert state["runtime_confidentiality_violation_intent_recorded"] is True
    assert state["runtime_home_cleanup_required"] is False
    assert state["runtime_home_cleanup_completed"] is True
    assert state["auth_confidentiality_violation_detected"] is True
    entries = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    assert sum(
        entry["event_type"]
        == "runtime_confidentiality_violation_intent"
        for entry in entries
    ) == 1
    assert sum(
        entry["event_type"]
        == "runtime_confidentiality_cleanup_completion"
        for entry in entries
    ) == 1
    quarantine_entries = {
        path.name for path in (run_directory / "quarantine").iterdir()
    }
    assert quarantine_entries == {"workspace", "codex-home"}
    assert not tuple(runtime_home.iterdir())
    raw_run = _raw_runtime_tree(run_directory)
    assert secret.encode("utf-8") not in raw_run
    assert detached.name.encode("utf-8") not in raw_run
    assert replacement_name.encode("utf-8") not in raw_run
    authoritative_after = {
        path.relative_to(authoritative): path.read_bytes()
        for path in authoritative.rglob("*")
        if path.is_file()
    }
    assert authoritative_after == authoritative_before


@pytest.mark.parametrize(
    ("crash_point", "occurrence"),
    (
        ("after_runtime_confidentiality_violation_journal_append", 1),
        ("after_result_replacement", 1),
        ("after_state_replacement", 1),
        ("before_runtime_home_scrub", 1),
        ("during_runtime_home_scrub", 1),
        ("after_runtime_home_scrub_before_cleanup_completion", 1),
        ("after_runtime_cleanup_completion_journal_append", 1),
        ("after_result_replacement", 2),
    ),
)
def test_confidentiality_cleanup_crash_boundaries_recover_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
    occurrence: int,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    services = replace(
        services,
        codex_authentication_file=tmp_path / "fake-auth.json",
    )
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
    launches_before = (tmp_path / "live-shadow-counter").read_text(
        encoding="ascii"
    )
    secret = (
        f"CRASH-CLEANUP-{occurrence}-"
        f"{hashlib.sha256(os.urandom(32)).hexdigest()}"
    )
    (tmp_path / "fake-auth.json").write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    runtime_home = run_directory / "quarantine" / "codex-home"
    secret_name = secret.encode("utf-8").hex()
    (runtime_home / secret_name).write_text(secret, encoding="utf-8")
    review = write_review(
        tmp_path / "review.yaml",
        "worker_initial-r000-a001",
    )
    seen = 0

    def crash(point: str) -> None:
        nonlocal seen
        if point != crash_point:
            return
        seen += 1
        if seen == occurrence:
            raise RuntimeError(f"crash at {point}")

    monkeypatch.setattr(engine, "_snapshot_checkpoint", crash)
    with pytest.raises(RuntimeError, match="crash at"):
        record_live_shadow_review(
            run_directory,
            "worker_initial-r000-a001",
            review,
            services=services,
        )
    assert seen == occurrence
    journal_after_crash = (
        run_directory / "journal.jsonl"
    ).read_bytes()
    entries_after_crash = [
        json.loads(line)
        for line in journal_after_crash.decode("ascii").splitlines()
    ]
    assert sum(
        entry["event_type"]
        == "runtime_confidentiality_violation_intent"
        for entry in entries_after_crash
    ) == 1
    assert not (
        run_directory / "reviews/worker_initial-r000-a001.json"
    ).exists()
    state_before_status = (run_directory / "state.json").read_bytes()
    result_before_status = (run_directory / "result.json").read_bytes()
    with pytest.raises((LiveShadowIntegrityError, LiveShadowStateError)):
        live_shadow_status(run_directory, services=services)
    with pytest.raises((LiveShadowIntegrityError, LiveShadowStateError)):
        live_shadow_report(run_directory, services=services)
    assert (run_directory / "journal.jsonl").read_bytes() == journal_after_crash
    assert (run_directory / "state.json").read_bytes() == state_before_status
    assert (run_directory / "result.json").read_bytes() == result_before_status

    monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
    recovered = resume_live_shadow(run_directory, services=services)
    assert recovered.status == "shadow_degraded"
    assert recovered.supervisor_session_usable is False
    assert recovered.runtime_confidentiality_violation_intent_recorded is True
    assert recovered.runtime_home_cleanup_required is False
    assert recovered.runtime_home_cleanup_completed is True
    recovered_entries = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    assert sum(
        entry["event_type"]
        == "runtime_confidentiality_violation_intent"
        for entry in recovered_entries
    ) == 1
    assert sum(
        entry["event_type"]
        == "runtime_confidentiality_cleanup_completion"
        for entry in recovered_entries
    ) == 1
    assert not tuple(runtime_home.iterdir())
    raw_run = _raw_runtime_tree(run_directory)
    assert secret.encode("utf-8") not in raw_run
    assert secret_name.encode("ascii") not in raw_run
    assert (tmp_path / "live-shadow-counter").read_text(
        encoding="ascii"
    ) == launches_before
    authoritative_after = {
        path.relative_to(authoritative): path.read_bytes()
        for path in authoritative.rglob("*")
        if path.is_file()
    }
    assert authoritative_after == authoritative_before


def test_completed_runtime_contamination_recovers_to_stable_shadow_degraded(
    tmp_path: Path,
) -> None:
    (
        run_directory,
        authentication_file,
        services,
        completed,
    ) = _completed_live_shadow_run(tmp_path)
    authoritative = Path(str(completed.authoritative_stage2_run))
    authoritative_before = {
        path.relative_to(authoritative): path.read_bytes()
        for path in authoritative.rglob("*")
        if path.is_file()
    }
    immutable_before = {
        path.relative_to(run_directory): path.read_bytes()
        for directory_name in (
            "decisions",
            "proposals",
            "comparisons",
            "reviews",
        )
        for path in (run_directory / directory_name).rglob("*")
        if path.is_file()
    }
    launch_counts_before = (
        (tmp_path / "live-shadow-counter").read_text(encoding="ascii"),
        (tmp_path / "stage2/fake-counter").read_text(encoding="ascii"),
    )
    secret = (
        "COMPLETED-CONTAMINATION-"
        f"{hashlib.sha256(os.urandom(32)).hexdigest()}"
    )
    authentication_file.write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    runtime_home = run_directory / "quarantine/codex-home"
    protected_name = hashlib.sha256(
        secret.encode("utf-8")
    ).hexdigest()
    (runtime_home / protected_name).write_text(
        secret,
        encoding="utf-8",
    )

    recovered = resume_live_shadow(
        run_directory,
        services=services,
    )
    assert recovered.status == "shadow_degraded"
    assert live_shadow_exit_code(recovered.status) == 5
    assert recovered.authoritative_stage2_status == "completed"
    assert (
        recovered.authoritative_result_sha256
        == completed.authoritative_result_sha256
    )
    assert recovered.authoritative_pause_reason == (
        completed.authoritative_pause_reason
    )
    assert recovered.supervisor_session_usable is False
    assert recovered.runtime_confidentiality_violation_intent_recorded is True
    assert recovered.runtime_home_cleanup_required is False
    assert recovered.runtime_home_cleanup_completed is True
    assert recovered.readiness == "not_ready"
    assert recovered.automation_enabled is False
    assert recovered.shadow_failure_count == (
        completed.shadow_failure_count + 1
    )
    assert (
        recovered.proposal_count,
        recovered.comparison_count,
        recovered.review_count,
    ) == (
        completed.proposal_count,
        completed.comparison_count,
        completed.review_count,
    )
    state = json.loads(
        (run_directory / "state.json").read_text(encoding="utf-8")
    )
    result_value = json.loads(
        (run_directory / "result.json").read_text(encoding="utf-8")
    )
    assert state["status"] == result_value["status"] == "shadow_degraded"
    assert state["runtime_home_cleanup_reason"] == (
        "runtime_confidentiality_violation_after_completion"
    )
    assert state["shadow_failures"][-1]["reason"] == (
        "runtime_confidentiality_violation_after_completion"
    )
    entries = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    assert sum(
        entry["event_type"]
        == "runtime_confidentiality_violation_intent"
        for entry in entries
    ) == 1
    assert sum(
        entry["event_type"]
        == "runtime_confidentiality_cleanup_completion"
        for entry in entries
    ) == 1
    assert not tuple(runtime_home.iterdir())
    raw_run = _raw_runtime_tree(run_directory)
    assert secret.encode("utf-8") not in raw_run
    assert protected_name.encode("ascii") not in raw_run

    files_before_reads = {
        path.relative_to(run_directory): path.read_bytes()
        for path in run_directory.rglob("*")
        if path.is_file()
    }
    status = live_shadow_status(run_directory, services=services)
    report = live_shadow_report(run_directory, services=services)
    assert status == recovered
    assert report["status"] == status.status
    assert report["authoritative"]["status"] == "completed"
    assert report["readiness"]["status"] == status.readiness
    assert (
        report["authentication_confidentiality"][
            "supervisor_session_usable"
        ]
        is False
    )
    assert len(report["shadow_failures"]) == status.shadow_failure_count
    assert len(report["assessments"]) == status.comparison_count
    assert len(report["comparisons"]) == status.comparison_count
    assert (
        report["readiness"]["reviewed_proposal_count"]
        == status.review_count
    )
    assert report["readiness"]["proposal_count"] == status.proposal_count
    rendered_reads = json.dumps(
        {
            "status": status.model_dump(mode="json"),
            "report": report,
        },
        sort_keys=True,
    )
    assert secret not in rendered_reads
    assert protected_name not in rendered_reads
    assert {
        path.relative_to(run_directory): path.read_bytes()
        for path in run_directory.rglob("*")
        if path.is_file()
    } == files_before_reads

    journal_before_second = (run_directory / "journal.jsonl").read_bytes()
    second = resume_live_shadow(
        run_directory,
        services=services,
    )
    assert second == recovered
    assert (run_directory / "journal.jsonl").read_bytes() == (
        journal_before_second
    )
    second_entries = [
        json.loads(line)
        for line in journal_before_second.decode("ascii").splitlines()
    ]
    assert sum(
        entry["event_type"]
        == "runtime_confidentiality_violation_intent"
        for entry in second_entries
    ) == 1
    assert sum(
        entry["event_type"]
        == "runtime_confidentiality_cleanup_completion"
        for entry in second_entries
    ) == 1
    assert second.shadow_failure_count == recovered.shadow_failure_count
    assert (
        (tmp_path / "live-shadow-counter").read_text(encoding="ascii"),
        (tmp_path / "stage2/fake-counter").read_text(encoding="ascii"),
    ) == launch_counts_before
    assert {
        path.relative_to(authoritative): path.read_bytes()
        for path in authoritative.rglob("*")
        if path.is_file()
    } == authoritative_before
    assert {
        path.relative_to(run_directory): path.read_bytes()
        for directory_name in (
            "decisions",
            "proposals",
            "comparisons",
            "reviews",
        )
        for path in (run_directory / directory_name).rglob("*")
        if path.is_file()
    } == immutable_before


@pytest.mark.parametrize(
    "crash_point",
    (
        "after_runtime_confidentiality_violation_journal_append",
        "after_runtime_confidentiality_session_invalidation_state",
        "after_runtime_home_scrub_before_cleanup_completion",
        "after_runtime_cleanup_completion_journal_append",
        "after_result_replacement",
    ),
)
def test_completed_contamination_crash_boundaries_recover_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    (
        run_directory,
        authentication_file,
        services,
        completed,
    ) = _completed_live_shadow_run(tmp_path)
    authoritative = Path(str(completed.authoritative_stage2_run))
    authoritative_before = {
        path.relative_to(authoritative): path.read_bytes()
        for path in authoritative.rglob("*")
        if path.is_file()
    }
    launch_counts_before = (
        (tmp_path / "live-shadow-counter").read_text(encoding="ascii"),
        (tmp_path / "stage2/fake-counter").read_text(encoding="ascii"),
    )
    secret = (
        f"COMPLETED-CRASH-{crash_point}-"
        f"{hashlib.sha256(os.urandom(32)).hexdigest()}"
    )
    authentication_file.write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    runtime_home = run_directory / "quarantine/codex-home"
    protected_name = hashlib.sha256(
        secret.encode("utf-8")
    ).hexdigest()
    (runtime_home / protected_name).write_text(
        secret,
        encoding="utf-8",
    )
    crashed = False

    def inject(point: str) -> None:
        nonlocal crashed
        if not crashed and point == crash_point:
            crashed = True
            raise RuntimeError(f"crash at {point}")

    monkeypatch.setattr(engine, "_snapshot_checkpoint", inject)
    with pytest.raises(RuntimeError, match="crash at"):
        resume_live_shadow(run_directory, services=services)
    assert crashed is True
    with pytest.raises(
        (LiveShadowIntegrityError, LiveShadowStateError)
    ):
        live_shadow_status(run_directory, services=services)
    with pytest.raises(
        (LiveShadowIntegrityError, LiveShadowStateError)
    ):
        live_shadow_report(run_directory, services=services)

    monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
    recovered = resume_live_shadow(
        run_directory,
        services=services,
    )
    assert recovered.status == "shadow_degraded"
    assert recovered.authoritative_stage2_status == "completed"
    assert recovered.supervisor_session_usable is False
    assert recovered.runtime_home_cleanup_required is False
    assert recovered.runtime_home_cleanup_completed is True
    assert recovered.shadow_failure_count == (
        completed.shadow_failure_count + 1
    )
    assert live_shadow_status(
        run_directory,
        services=services,
    ) == recovered
    assert live_shadow_report(
        run_directory,
        services=services,
    )["status"] == "shadow_degraded"
    entries = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    assert sum(
        entry["event_type"]
        == "runtime_confidentiality_violation_intent"
        for entry in entries
    ) == 1
    assert sum(
        entry["event_type"]
        == "runtime_confidentiality_cleanup_completion"
        for entry in entries
    ) == 1
    assert sum(
        failure["reason"]
        == "runtime_confidentiality_violation_after_completion"
        for failure in json.loads(
            (run_directory / "state.json").read_text(encoding="utf-8")
        )["shadow_failures"]
    ) == 1
    journal_before_second = (run_directory / "journal.jsonl").read_bytes()
    assert resume_live_shadow(
        run_directory,
        services=services,
    ) == recovered
    assert (run_directory / "journal.jsonl").read_bytes() == (
        journal_before_second
    )
    assert not tuple(runtime_home.iterdir())
    assert secret.encode("utf-8") not in _raw_runtime_tree(run_directory)
    assert (
        (tmp_path / "live-shadow-counter").read_text(encoding="ascii"),
        (tmp_path / "stage2/fake-counter").read_text(encoding="ascii"),
    ) == launch_counts_before
    assert {
        path.relative_to(authoritative): path.read_bytes()
        for path in authoritative.rglob("*")
        if path.is_file()
    } == authoritative_before


def test_review_validates_and_scrubs_authentication_paths_before_mutation(
    tmp_path: Path,
) -> None:
    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    services = replace(
        services,
        codex_authentication_file=tmp_path / "fake-auth.json",
    )
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    launches_before = (tmp_path / "live-shadow-counter").read_text(
        encoding="ascii"
    )
    authoritative = Path(str(result.authoritative_stage2_run))
    authoritative_before = {
        path.relative_to(authoritative): path.read_bytes()
        for path in authoritative.rglob("*")
        if path.is_file()
    }
    secret = f"review-auth-{hashlib.sha256(os.urandom(32)).hexdigest()}"
    auth = tmp_path / "fake-auth.json"
    auth.write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    runtime_home = run_directory / "quarantine" / "codex-home"
    contaminated = runtime_home / secret
    contaminated.mkdir()
    (contaminated / "safe").write_bytes(b"safe")
    review = write_review(
        tmp_path / "review.yaml",
        "worker_initial-r000-a001",
    )
    with pytest.raises(
        LiveShadowIntegrityError,
        match="confidentiality boundary",
    ) as captured:
        record_live_shadow_review(
            run_directory,
            "worker_initial-r000-a001",
            review,
            services=services,
        )
    assert secret not in str(captured.value)
    assert not (
        run_directory / "reviews/worker_initial-r000-a001.json"
    ).exists()
    assert not contaminated.exists()
    state = json.loads(
        (run_directory / "state.json").read_text(encoding="utf-8")
    )
    assert state["supervisor_session_usable"] is False
    assert state["auth_confidentiality_violation_detected"] is True
    assert (tmp_path / "live-shadow-counter").read_text(
        encoding="ascii"
    ) == launches_before
    authoritative_after = {
        path.relative_to(authoritative): path.read_bytes()
        for path in authoritative.rglob("*")
        if path.is_file()
    }
    assert authoritative_after == authoritative_before
    assert secret.encode() not in _raw_runtime_tree(run_directory)


def test_abort_scrubs_runtime_authentication_content_and_paths_before_commit(
    tmp_path: Path,
) -> None:
    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    services = replace(
        services,
        codex_authentication_file=tmp_path / "fake-auth.json",
    )
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    secret = f"abort-auth-{hashlib.sha256(os.urandom(32)).hexdigest()}"
    (tmp_path / "fake-auth.json").write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    runtime_home = run_directory / "quarantine" / "codex-home"
    contaminated = runtime_home / f"Bearer {secret}"
    contaminated.write_text(secret, encoding="utf-8")
    aborted = abort_live_shadow(
        run_directory,
        "operator stop",
        services=services,
    )
    assert aborted.status == "aborted"
    assert aborted.authoritative_stage2_status == "completed"
    assert aborted.supervisor_session_usable is False
    assert aborted.auth_confidentiality_violation_detected is True
    assert not contaminated.exists()
    assert secret.encode() not in _raw_runtime_tree(run_directory)
    assert live_shadow_status(
        run_directory,
        services=services,
    ).status == "aborted"


def test_abort_cleanup_failure_is_integrity_without_abort_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.live_shadow_isolation as isolation

    spec, _, _, _, services = create_live_shadow_tree(tmp_path)
    services = replace(
        services,
        codex_authentication_file=tmp_path / "fake-auth.json",
    )
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    secret = f"cleanup-auth-{hashlib.sha256(os.urandom(32)).hexdigest()}"
    (tmp_path / "fake-auth.json").write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    runtime_home = run_directory / "quarantine" / "codex-home"
    (runtime_home / secret).write_bytes(b"safe")
    journal_before = (run_directory / "journal.jsonl").read_bytes()

    def fail_cleanup(*_: object, **__: object) -> None:
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(
        isolation,
        "_remove_runtime_entry_at",
        fail_cleanup,
    )
    with pytest.raises(
        LiveShadowIntegrityError,
        match="could not be scrubbed",
    ) as captured:
        abort_live_shadow(
            run_directory,
            "operator stop",
            services=services,
        )
    assert secret not in str(captured.value)
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
    assert not any(
        entry["event_type"]
        == "runtime_confidentiality_cleanup_completion"
        for entry in entries
    )
    state = json.loads(
        (run_directory / "state.json").read_text(encoding="utf-8")
    )
    assert state["status"] == "shadow_degraded"
    assert state["supervisor_session_usable"] is False
    assert state["runtime_home_cleanup_required"] is True


def test_abort_in_flight_stops_only_shadow_before_runtime_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        supervisor_responses=[
            live_supervisor_response("worker_initial", sleep_seconds=10),
        ],
        stage2_responses=[
            codex_response(
                "worker",
                SOURCE_WORKER_UUID,
                worker_result(),
                sleep_seconds=2,
            ),
            codex_response(
                "auditor",
                SOURCE_AUDITOR_UUID,
                auditor_result(),
            ),
        ],
    )
    services = replace(
        services,
        codex_authentication_file=tmp_path / "fake-auth.json",
    )
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = run_live_shadow(
                spec,
                runs_dir=tmp_path / "live-runs",
                stage2_runs_dir=tmp_path / "stage2-runs",
                services=services,
            )
        except BaseException as exc:
            outcome["error"] = exc

    observer = threading.Thread(target=run, daemon=True)
    observer.start()
    process_record: dict[str, object] | None = None
    run_directory: Path | None = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        candidates = tuple((tmp_path / "live-runs").glob("*"))
        if candidates:
            run_directory = candidates[0]
            process_path = run_directory / "supervisor-process.json"
            if process_path.is_file():
                process_record = json.loads(
                    process_path.read_text(encoding="utf-8")
                )
                break
        time.sleep(0.005)
    assert run_directory is not None
    assert process_record is not None
    launch = json.loads(
        (run_directory / "authoritative" / "launch.json").read_text(
            encoding="utf-8"
        )
    )
    shadow_pid = int(process_record["process_id"])
    shadow_ticks = int(process_record["process_start_ticks"])
    stage2_pid = int(launch["pid"])
    stage2_ticks = int(launch["process_start_ticks"])
    assert engine._process_identity_running(shadow_pid, shadow_ticks)
    assert engine._process_identity_running(stage2_pid, stage2_ticks)

    secret = f"abort-in-flight-{hashlib.sha256(os.urandom(32)).hexdigest()}"
    (tmp_path / "fake-auth.json").write_text(
        json.dumps({"access_token": secret}),
        encoding="utf-8",
    )
    runtime_home = run_directory / "quarantine" / "codex-home"
    (runtime_home / secret).write_text(secret, encoding="utf-8")
    scanned = False
    original_validation = engine._stable_runtime_home_validation

    def assert_writer_stopped(
        *args: object,
        **kwargs: object,
    ) -> tuple[tuple[str, ...], str | None, object]:
        nonlocal scanned
        scanned = True
        assert not engine._process_identity_running(shadow_pid, shadow_ticks)
        assert engine._process_identity_running(stage2_pid, stage2_ticks)
        return original_validation(*args, **kwargs)

    monkeypatch.setattr(
        engine,
        "_stable_runtime_home_validation",
        assert_writer_stopped,
    )
    aborted = abort_live_shadow(
        run_directory,
        "operator stop",
        services=services,
    )
    assert aborted.status == "aborted"
    assert scanned is True
    assert aborted.supervisor_session_usable is False
    assert aborted.auth_confidentiality_violation_detected is True
    assert engine._process_identity_running(stage2_pid, stage2_ticks)
    assert not engine._process_identity_running(shadow_pid, shadow_ticks)
    assert not (run_directory / "supervisor-process.json").exists()
    assert secret.encode() not in _raw_runtime_tree(run_directory)
    observer.join(timeout=10)
    assert not observer.is_alive()
    assert "error" not in outcome


def test_supervisor_launch_cannot_cross_runtime_validation_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        supervisor_responses=[
            live_supervisor_response(
                "worker_initial",
                sleep_seconds=1,
            ),
            live_supervisor_response("auditor", resume=True),
        ],
    )
    entered = threading.Event()
    release = threading.Event()
    original_inspect = engine.inspect_runtime_home_contents

    def block_launch_validation(
        runtime_home: Path,
        **kwargs: object,
    ) -> tuple[str, ...]:
        findings = original_inspect(runtime_home, **kwargs)
        run_directory = runtime_home.parent.parent
        state_path = run_directory / "state.json"
        if (
            not entered.is_set()
            and state_path.is_file()
        ):
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if (
                state["observed_decision_ids"]
                and state["pending_action"] is None
                and not state["proposal_ids"]
            ):
                entered.set()
                assert release.wait(timeout=10)
        return findings

    monkeypatch.setattr(
        engine,
        "inspect_runtime_home_contents",
        block_launch_validation,
    )
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = run_live_shadow(
                spec,
                runs_dir=tmp_path / "live-runs",
                stage2_runs_dir=tmp_path / "stage2-runs",
                services=services,
            )
        except BaseException as exc:
            outcome["error"] = exc

    observer = threading.Thread(target=run, daemon=True)
    observer.start()
    assert entered.wait(timeout=10)
    assert not (tmp_path / "live-shadow-counter").exists()
    run_directory = next((tmp_path / "live-runs").iterdir())
    assert not (run_directory / "supervisor-process.json").exists()
    release.set()
    observer.join(timeout=15)
    assert not observer.is_alive()
    assert "error" not in outcome
    assert isinstance(outcome["result"], LiveShadowResult)
    assert outcome["result"].status in {
        "awaiting_reviews",
        "shadow_degraded",
    }
    assert int(
        (tmp_path / "live-shadow-counter").read_text(encoding="ascii")
    ) >= 1


def test_review_never_scans_across_active_supervisor_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        supervisor_responses=[
            live_supervisor_response(
                "worker_initial",
                sleep_seconds=1,
            ),
            live_supervisor_response("auditor", resume=True),
        ],
        stage2_responses=[
            codex_response(
                "worker",
                SOURCE_WORKER_UUID,
                worker_result(),
                sleep_seconds=2,
            ),
            codex_response(
                "auditor",
                SOURCE_AUDITOR_UUID,
                auditor_result(),
            ),
        ],
    )
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["result"] = run_live_shadow(
                spec,
                runs_dir=tmp_path / "live-runs",
                stage2_runs_dir=tmp_path / "stage2-runs",
                services=services,
            )
        except BaseException as exc:
            outcome["error"] = exc

    observer = threading.Thread(target=run, daemon=True)
    observer.start()
    run_directory: Path | None = None
    process_record: dict[str, object] | None = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        candidates = tuple((tmp_path / "live-runs").glob("*"))
        if candidates:
            run_directory = candidates[0]
            process_path = run_directory / "supervisor-process.json"
            if process_path.is_file():
                process_record = json.loads(
                    process_path.read_text(encoding="utf-8")
                )
                break
        time.sleep(0.005)
    assert run_directory is not None
    assert process_record is not None
    counter_paths = (
        tmp_path / "live-shadow-counter",
        tmp_path / "stage2/fake-counter",
    )
    deadline = time.monotonic() + 5
    while (
        not all(path.is_file() for path in counter_paths)
        and time.monotonic() < deadline
    ):
        time.sleep(0.005)
    assert all(path.is_file() for path in counter_paths)
    scanner_names = (
        "validate_runtime_home_contents",
        "inspect_runtime_home_contents",
        "_validate_quarantine_workspace",
        "_validate_run",
        "_load_reconciled_run",
        "_stable_runtime_home_validation",
    )
    scan_calls = dict.fromkeys(scanner_names, 0)
    for scanner_name in scanner_names:
        original = getattr(engine, scanner_name)

        def instrument(
            *args: object,
            _name: str = scanner_name,
            _original: object = original,
            **kwargs: object,
        ) -> object:
            scan_calls[_name] += 1
            assert callable(_original)
            return _original(*args, **kwargs)

        monkeypatch.setattr(engine, scanner_name, instrument)
    review = write_review(
        tmp_path / "review-active.yaml",
        "worker_initial-r000-a001",
    )
    durable_paths = tuple(
        run_directory / name
        for name in ("journal.jsonl", "state.json", "result.json")
    )
    durable_hashes = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in durable_paths
    }
    state_before = json.loads(
        (run_directory / "state.json").read_text(encoding="utf-8")
    )
    review_count_before = len(state_before["reviewed_proposal_ids"])
    launch_counts_before = (
        counter_paths[0].read_text(encoding="ascii"),
        counter_paths[1].read_text(encoding="ascii"),
    )
    launch = json.loads(
        (run_directory / "authoritative/launch.json").read_text(
            encoding="utf-8"
        )
    )
    stage2_pid = int(launch["pid"])
    stage2_ticks = int(launch["process_start_ticks"])
    assert engine._process_identity_running(stage2_pid, stage2_ticks)
    deadline = time.monotonic() + 2
    while True:
        try:
            record_live_shadow_review(
                run_directory,
                "worker_initial-r000-a001",
                review,
                services=services,
            )
        except LiveShadowLockError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.001)
            continue
        except LiveShadowInputError as exc:
            assert "still in flight" in str(exc)
            break
        raise AssertionError("review unexpectedly crossed the active writer")
    assert scan_calls == dict.fromkeys(scanner_names, 0)
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in durable_paths
    } == durable_hashes
    state_after = json.loads(
        (run_directory / "state.json").read_text(encoding="utf-8")
    )
    assert len(state_after["reviewed_proposal_ids"]) == review_count_before
    assert not (
        run_directory / "reviews/worker_initial-r000-a001.json"
    ).exists()
    assert (
        counter_paths[0].read_text(encoding="ascii"),
        counter_paths[1].read_text(encoding="ascii"),
    ) == launch_counts_before
    pid = int(process_record["process_id"])
    ticks = int(process_record["process_start_ticks"])
    assert engine._process_identity_running(pid, ticks)
    assert engine._process_identity_running(stage2_pid, stage2_ticks)
    observer.join(timeout=15)
    assert not observer.is_alive()
    assert "error" not in outcome
    for scanner_name in scanner_names:
        scan_calls[scanner_name] = 0
    launch_counts_after_observation = (
        counter_paths[0].read_text(encoding="ascii"),
        counter_paths[1].read_text(encoding="ascii"),
    )
    reviewed = record_live_shadow_review(
        run_directory,
        "worker_initial-r000-a001",
        review,
        services=services,
    )
    assert reviewed.review_count == review_count_before + 1
    assert scan_calls == dict.fromkeys(scanner_names, 1)
    assert (
        counter_paths[0].read_text(encoding="ascii"),
        counter_paths[1].read_text(encoding="ascii"),
    ) == launch_counts_after_observation


def _prepared_supervisor(
    tmp_path: Path,
) -> tuple[
    PreparedCodexRequest,
    Path,
    Path,
    Path,
    Path,
    BubblewrapCapability,
]:
    run_root = tmp_path / "live-run"
    workspace = run_root / "quarantine" / "workspace"
    runtime_home = run_root / "quarantine" / "codex-home"
    schema = run_root / "decisions" / "worker_initial-r000-a001" / "output-schema.json"
    action = tmp_path / "action"
    codex = tmp_path / "codex"
    auth = tmp_path / "auth.json"
    prompt = tmp_path / "policy.md"
    for directory in (workspace, runtime_home, schema.parent, action):
        directory.mkdir(parents=True, exist_ok=True)
    schema.write_text('{"type":"object"}\n', encoding="ascii")
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    auth.write_text('{"subscription":"unit-test-secret"}\n', encoding="ascii")
    prompt.write_text("Supervisor policy.\n", encoding="utf-8")
    request = CodexRunRequest(
        schema_version=1,
        run_id="stage1-run",
        role="supervisor",
        workspace=str(workspace),
        prompt_path=str(prompt),
        model="gpt-5.6-sol",
        reasoning_effort="high",
        timeout_seconds=60,
    )
    prepared = PreparedCodexRequest(
        request_path=tmp_path / "live-shadow.yaml",
        request=request,
        workspace=workspace,
        prompt_path=prompt,
        prompt_bytes=b"blind supervisor input\n",
        prompt_sha256=hashlib.sha256(b"blind supervisor input\n").hexdigest(),
        policy=ROLE_POLICIES["supervisor"],
    )
    capability = BubblewrapCapability(
        identity=BubblewrapBackendIdentity(
            schema_version=1,
            isolation_schema_version=1,
            backend="bubblewrap",
            canonical_bubblewrap_path="/usr/bin/bwrap",
            bubblewrap_version="bubblewrap 0.11.1",
            capability_result="passed",
        ),
        authentication_file=auth,
    )
    return prepared, run_root, runtime_home, schema, action, capability


def _production_launch(
    tmp_path: Path,
    *,
    resume: bool = False,
) -> tuple[CodexProcessLaunch, PreparedCodexRequest, Path, Path, Path, Path]:
    prepared, run_root, runtime_home, schema, action, capability = (
        _prepared_supervisor(tmp_path)
    )
    final_message = action / "last-message.md"
    semantic = build_codex_command(
        prepared,
        str(tmp_path / "codex"),
        final_message,
        output_schema=schema,
        resume_thread_id=SUPERVISOR_UUID if resume else None,
        skip_git_repo_check=True,
    )
    repository = tmp_path / "authoritative-repository"
    stage2_run = tmp_path / "authoritative-stage2-run"
    repository.mkdir()
    stage2_run.mkdir()
    launch = build_bubblewrap_process_launch(
        semantic,
        prepared,
        {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"},
        final_message,
        schema,
        capability=capability,
        stage4_run_root=run_root,
        runtime_home=runtime_home,
        forbidden_roots=(repository, stage2_run),
    )
    return launch, prepared, run_root, schema, action, runtime_home


def _mount_arguments(command: tuple[str, ...]) -> list[tuple[str, str, str]]:
    result: list[tuple[str, str, str]] = []
    index = 0
    while index < command.index("--"):
        if command[index] in {"--bind", "--ro-bind"}:
            result.append(
                (command[index], command[index + 1], command[index + 2])
            )
            index += 3
        else:
            index += 1
    return result


def test_production_bubblewrap_argv_is_a_synthetic_allowlist(
    tmp_path: Path,
) -> None:
    launch, prepared, _, schema, action, runtime_home = _production_launch(
        tmp_path
    )
    command = launch.command
    separator = command.index("--")
    prefix = command[:separator]
    nested = command[separator + 1 :]

    assert command[0] == "/usr/bin/bwrap"
    assert prefix[1:15] == (
        "--die-with-parent",
        "--new-session",
        "--unshare-user",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup-try",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
    )
    assert "--unshare-net" not in command
    assert nested[0] == ISOLATED_CODEX_PATH
    assert nested[nested.index("--cd") + 1] == ISOLATED_WORKSPACE_PATH
    assert (
        nested[nested.index("--output-last-message") + 1]
        == f"{ISOLATED_ACTION_DIRECTORY}/last-message.md"
    )
    assert (
        nested[nested.index("--output-schema") + 1]
        == ISOLATED_OUTPUT_SCHEMA_PATH
    )
    assert str(prepared.workspace) not in nested
    assert str(schema) not in nested
    assert str(action) not in nested
    mounts = _mount_arguments(command)
    assert ("--ro-bind", str(prepared.workspace), "/workspace") in mounts
    assert ("--bind", str(action), "/action") in mounts
    assert ("--ro-bind", str(schema), "/control/output-schema.json") in mounts
    assert ("--bind", str(runtime_home), "/home/supervisor") in mounts


def test_mounts_never_bind_host_root_home_mnt_or_authority(
    tmp_path: Path,
) -> None:
    launch, _, _, _, _, _ = _production_launch(tmp_path)
    mounts = _mount_arguments(launch.command)
    sources = {source for _, source, _ in mounts}
    destinations = {destination for _, _, destination in mounts}
    forbidden_sources = {
        "/",
        "/home",
        "/mnt",
        str(tmp_path / "authoritative-repository"),
        str(tmp_path / "authoritative-stage2-run"),
    }
    assert not sources.intersection(forbidden_sources)
    assert "/" not in destinations
    assert "/home" not in destinations
    assert "/mnt" not in destinations
    assert "--ro-bind" in launch.command
    assert ("--ro-bind", "/", "/") not in mounts


def test_isolated_environment_and_only_two_writable_mounts(
    tmp_path: Path,
) -> None:
    launch, _, _, _, action, runtime_home = _production_launch(tmp_path)
    assert launch.environment["HOME"] == ISOLATED_HOME
    assert launch.environment["CODEX_HOME"] == ISOLATED_HOME
    assert launch.environment["TMPDIR"] == ISOLATED_TMPDIR
    assert launch.environment["LANG"] == "C.UTF-8"
    assert {
        (source, destination)
        for option, source, destination in _mount_arguments(launch.command)
        if option == "--bind"
    } == {
        (str(action), ISOLATED_ACTION_DIRECTORY),
        (str(runtime_home), ISOLATED_HOME),
    }


def test_initial_and_exact_uuid_resume_use_the_same_isolation(
    tmp_path: Path,
) -> None:
    initial, *_ = _production_launch(tmp_path / "initial")
    resumed, *_ = _production_launch(tmp_path / "resumed", resume=True)
    for command in (initial.command, resumed.command):
        assert command[0] == "/usr/bin/bwrap"
        assert "--proc" in command
        assert "--dev" in command
        assert "--tmpfs" in command
        assert "--unshare-net" not in command
        nested = command[command.index("--") + 1 :]
        exec_index = nested.index("exec")
        assert nested.count("--skip-git-repo-check") == 1
        assert nested.index("--skip-git-repo-check") == exec_index + 1
    nested = resumed.command[resumed.command.index("--") + 1 :]
    resume_index = nested.index("resume")
    assert resume_index == nested.index("exec") + 2
    assert nested[resume_index + 1] == SUPERVISOR_UUID
    assert "--last" not in nested
    assert "--all" not in nested


@pytest.mark.parametrize("mutation", ("omit", "duplicate", "misplace"))
def test_stage4_launch_rejects_invalid_skip_git_repo_check(
    tmp_path: Path,
    mutation: str,
) -> None:
    prepared, run_root, runtime_home, schema, action, capability = (
        _prepared_supervisor(tmp_path)
    )
    final_message = action / "last-message.md"
    semantic = build_codex_command(
        prepared,
        str(tmp_path / "codex"),
        final_message,
        output_schema=schema,
        skip_git_repo_check=True,
    )
    flag_index = semantic.index("--skip-git-repo-check")
    if mutation == "omit":
        semantic.pop(flag_index)
    elif mutation == "duplicate":
        semantic.insert(flag_index + 1, "--skip-git-repo-check")
    else:
        semantic.pop(flag_index)
        semantic.insert(semantic.index("--json") + 1, "--skip-git-repo-check")
    repository = tmp_path / "repository"
    stage2_run = tmp_path / "stage2-run"
    repository.mkdir()
    stage2_run.mkdir()

    with pytest.raises(LiveShadowIntegrityError, match="skip-git-repo-check"):
        build_bubblewrap_process_launch(
            semantic,
            prepared,
            {},
            final_message,
            schema,
            capability=capability,
            stage4_run_root=run_root,
            runtime_home=runtime_home,
            forbidden_roots=(repository, stage2_run),
        )


def test_mount_rejects_symlink_into_authoritative_repository(
    tmp_path: Path,
) -> None:
    prepared, run_root, runtime_home, schema, action, capability = (
        _prepared_supervisor(tmp_path)
    )
    repository = tmp_path / "repository"
    repository.mkdir()
    real_workspace = repository / "stolen"
    real_workspace.mkdir()
    prepared.workspace.rmdir()
    prepared.workspace.symlink_to(real_workspace, target_is_directory=True)
    semantic = build_codex_command(
        prepared,
        str(tmp_path / "codex"),
        action / "last-message.md",
        output_schema=schema,
        skip_git_repo_check=True,
    )
    with pytest.raises(LiveShadowIntegrityError, match="symlink"):
        build_bubblewrap_process_launch(
            semantic,
            prepared,
            {},
            action / "last-message.md",
            schema,
            capability=capability,
            stage4_run_root=run_root,
            runtime_home=runtime_home,
            forbidden_roots=(repository, tmp_path / "stage2-run"),
        )


def test_mount_rejects_duplicate_and_overlapping_destinations(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    action = tmp_path / "action"
    schema = tmp_path / "schema.json"
    runtime = tmp_path / "runtime"
    forbidden_a = tmp_path / "repository"
    forbidden_b = tmp_path / "stage2-run"
    for directory in (
        source,
        workspace,
        action,
        runtime,
        forbidden_a,
        forbidden_b,
    ):
        directory.mkdir()
    schema.write_text("{}\n", encoding="ascii")
    duplicate = (
        _Mount("--bind", action, "/action", "action-output"),
        _Mount("--bind", runtime, "/home/supervisor", "codex-runtime-home"),
        _Mount("--ro-bind", source, "/workspace", "one"),
        _Mount("--ro-bind", source, "/workspace", "two"),
    )
    with pytest.raises(LiveShadowIntegrityError, match="duplicate"):
        _validate_mount_allowlist(
            duplicate,
            forbidden_roots=(forbidden_a, forbidden_b),
            stage4_run_root=None,
            workspace=workspace,
            action_directory=action,
            output_schema=schema,
            runtime_home=runtime,
        )


def test_real_preflight_runs_without_invoking_codex(
    tmp_path: Path,
) -> None:
    codex = tmp_path / "must-not-run"
    codex.write_text("#!/bin/sh\nexit 99\n", encoding="ascii")
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="ascii")
    repository = tmp_path / "repository"
    stage2 = tmp_path / "stage2-run"
    repository.mkdir()
    stage2.mkdir()
    capability = preflight_bubblewrap_isolation(
        bubblewrap_executable="/usr/bin/bwrap",
        codex_executable=str(codex),
        authentication_file=auth,
        environ={},
        forbidden_roots=(repository, stage2),
    )
    assert capability.identity.capability_result == "passed"
    assert capability.identity.bubblewrap_version.startswith("bubblewrap ")


def test_authentication_material_is_absent_from_dependency_errors(
    tmp_path: Path,
) -> None:
    secret = "AUTH-CONTENTS-MUST-NEVER-BE-REPORTED"
    missing_auth = tmp_path / secret / "auth.json"
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
    repository = tmp_path / "repository"
    stage2 = tmp_path / "stage2"
    repository.mkdir()
    stage2.mkdir()
    with pytest.raises(LiveShadowDependencyError) as captured:
        preflight_bubblewrap_isolation(
            bubblewrap_executable="/usr/bin/bwrap",
            codex_executable=str(codex),
            authentication_file=missing_auth,
            environ={},
            forbidden_roots=(repository, stage2),
        )
    assert secret not in str(captured.value)


def test_missing_bubblewrap_fails_before_stage2_or_supervisor_launch(
    tmp_path: Path,
) -> None:
    spec, _, _, _, base_services = create_live_shadow_tree(tmp_path)
    supervisor_launches = 0

    def forbidden_supervisor(*_: object, **__: object) -> Any:
        nonlocal supervisor_launches
        supervisor_launches += 1
        raise AssertionError("unisolated fallback launched")

    services = replace(
        base_services,
        supervisor_invoker=forbidden_supervisor,  # type: ignore[arg-type]
        isolation_preflight=preflight_bubblewrap_isolation,
        bubblewrap_executable="/usr/bin/definitely-missing-bwrap",
    )
    with pytest.raises(LiveShadowDependencyError, match="Bubblewrap"):
        run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    assert not (tmp_path / "live-runs").exists()
    assert not (tmp_path / "stage2-runs").exists()
    assert supervisor_launches == 0


def test_failed_capability_probe_fails_before_stage2_launch(
    tmp_path: Path,
) -> None:
    spec, _, _, _, base_services = create_live_shadow_tree(tmp_path)

    def failed_preflight(**_: object) -> BubblewrapCapability:
        raise LiveShadowDependencyError(
            "Bubblewrap synthetic-root capability probe failed"
        )

    services = replace(
        base_services,
        isolation_preflight=failed_preflight,
    )
    with pytest.raises(LiveShadowDependencyError, match="capability"):
        run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    assert not (tmp_path / "stage2-runs").exists()


def test_explicit_run_root_cannot_weaken_separation(
    tmp_path: Path,
) -> None:
    spec, _, project, _, services = create_live_shadow_tree(tmp_path)
    with pytest.raises(LiveShadowInputError, match="must be separate"):
        run_live_shadow(
            spec,
            runs_dir=project / "shadow-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    assert not (project / "shadow-runs").exists()


def test_resume_with_missing_backend_degrades_shadow_without_relaunching_stage2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import research_automation_supervisor.live_shadow_engine as engine

    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        stage2_responses=[
            codex_response(
                "worker",
                SOURCE_WORKER_UUID,
                worker_result(),
                sleep_seconds=2.0,
            ),
            codex_response(
                "auditor",
                SOURCE_AUDITOR_UUID,
                auditor_result(),
            ),
        ],
    )
    interrupted = False

    def crash_after_first_envelope(point: str) -> None:
        nonlocal interrupted
        if interrupted or point != "after_state_replacement":
            return
        journals = tuple((tmp_path / "live-runs").glob("*/journal.jsonl"))
        if not journals:
            return
        lines = journals[0].read_text(encoding="ascii").splitlines()
        if lines and json.loads(lines[-1])["reason"] == (
            "live_decision_envelope_frozen"
        ):
            interrupted = True
            raise RuntimeError("crash before isolated supervisor launch")

    monkeypatch.setattr(engine, "_snapshot_checkpoint", crash_after_first_envelope)
    with pytest.raises(RuntimeError, match="before isolated"):
        run_live_shadow(
            spec,
            runs_dir=tmp_path / "live-runs",
            stage2_runs_dir=tmp_path / "stage2-runs",
            services=services,
        )
    assert interrupted
    run_directory = next((tmp_path / "live-runs").iterdir())
    authoritative_result_path = next(
        (tmp_path / "stage2-runs").glob("*/result.json")
    )
    assert (
        json.loads(authoritative_result_path.read_text(encoding="utf-8"))[
            "status"
        ]
        == "worker_running"
    )

    clock = [datetime(2040, 1, 1, tzinfo=UTC)]

    def unavailable(**_: object) -> BubblewrapCapability:
        raise LiveShadowDependencyError("Bubblewrap disappeared")

    def advance(_: float) -> None:
        clock[0] += timedelta(seconds=31)
        time.sleep(0.001)

    monkeypatch.setattr(engine, "_snapshot_checkpoint", lambda _: None)
    recovered = resume_live_shadow(
        run_directory,
        services=replace(
            services,
            isolation_preflight=unavailable,
            utc_now=lambda: clock[0],
            sleep=advance,
        ),
    )
    assert recovered.status == "shadow_degraded"
    assert recovered.authoritative_stage2_status == "completed"
    assert recovered.observed_decision_count == 2
    assert recovered.proposal_count == 2
    journal = [
        json.loads(line)
        for line in (run_directory / "journal.jsonl")
        .read_text(encoding="ascii")
        .splitlines()
    ]
    assert sum(
        entry["reason"] == "authoritative_stage2_launched"
        for entry in journal
    ) == 1
    state = json.loads(
        (run_directory / "state.json").read_text(encoding="utf-8")
    )
    assert any(
        failure["reason"] == "isolation_dependency_failure"
        for failure in state["shadow_failures"]
    )
    assert not (tmp_path / "live-shadow-counter").exists()


def test_runtime_validation_ignores_authoritative_atomic_temporary_workload(
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
    authoritative = Path(str(result.authoritative_stage2_run))
    sentinel = os.urandom(32).hex().encode("ascii")
    stop = threading.Event()
    errors: list[BaseException] = []

    def write_atomic_temporaries() -> None:
        counter = 0
        try:
            while not stop.is_set():
                temporary = authoritative / f".ordinary-stage2-{counter}.tmp"
                destination = authoritative / "ordinary-stage2-volatile.tmp"
                temporary.write_bytes(sentinel)
                os.replace(temporary, destination)
                counter += 1
        except BaseException as exc:
            errors.append(exc)

    observed_fragment_sets: list[tuple[bytes, ...]] = []
    original_validate = engine.validate_runtime_home_contents

    def record_runtime_inputs(
        *args: object,
        **kwargs: object,
    ) -> None:
        fragments = tuple(
            kwargs.get("forbidden_fragments", ())
        )
        observed_fragment_sets.append(fragments)
        assert all(sentinel not in fragment for fragment in fragments)
        original_validate(*args, **kwargs)

    monkeypatch.setattr(
        engine,
        "validate_runtime_home_contents",
        record_runtime_inputs,
    )
    writer = threading.Thread(target=write_atomic_temporaries, daemon=True)
    writer.start()
    try:
        for _ in range(20):
            observed = live_shadow_status(
                run_directory,
                services=services,
            )
            assert observed.status == "awaiting_reviews"
            assert observed.proposal_count == result.proposal_count
            assert observed.comparison_count == result.comparison_count
    finally:
        stop.set()
        writer.join(timeout=2)
    assert not writer.is_alive()
    assert not errors
    assert observed_fragment_sets


def test_resumed_blind_stdin_excludes_prior_comparison_and_candidate(
    tmp_path: Path,
) -> None:
    candidate_sentinel = "PRIOR-CANDIDATE-MUST-NOT-ENTER-RESUMED-STDIN"
    first = live_supervisor_response("worker_initial")
    first_proposal = json.loads(str(first["final"]))
    first_proposal["prompt"] = candidate_sentinel
    first["final"] = json.dumps(first_proposal, sort_keys=True)
    first["observation_path"] = str(tmp_path / "first-observation.json")
    second = live_supervisor_response("auditor", resume=True)
    second["observation_path"] = str(tmp_path / "second-observation.json")
    spec, _, _, _, services = create_live_shadow_tree(
        tmp_path,
        supervisor_responses=[first, second],
    )
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    run_directory = Path(result.artifact_directory)
    resumed_prompt = base64.b64decode(
        json.loads(
            (tmp_path / "second-observation.json").read_text(encoding="utf-8")
        )["prompt_base64"]
    )
    comparison = (
        run_directory
        / "comparisons/worker_initial-r000-a001/comparison.json"
    ).read_bytes()
    assert candidate_sentinel.encode("utf-8") not in resumed_prompt
    assert comparison not in resumed_prompt
    assert b'"review_status":"reviewed"' not in resumed_prompt


def _write_isolation_fake(
    path: Path,
    *,
    authoritative_workspace: Path,
    authoritative_stage2_runs: Path,
    worker_release: Path,
    future_sentinel: str,
) -> None:
    workspace_b64 = base64.b64encode(
        str(authoritative_workspace).encode("utf-8")
    ).decode("ascii")
    release_b64 = base64.b64encode(
        str(worker_release).encode("utf-8")
    ).decode("ascii")
    stage2_b64 = base64.b64encode(
        str(authoritative_stage2_runs).encode("utf-8")
    ).decode("ascii")
    sentinel_b64 = base64.b64encode(future_sentinel.encode("utf-8")).decode(
        "ascii"
    )
    worker_final = worker_result()
    auditor_final = auditor_result()
    worker_proposal = supervisor_proposal("worker_initial")
    auditor_proposal = supervisor_proposal("auditor")
    source = f"""#!/usr/bin/python3
import base64
import json
import os
import sys
import time
from pathlib import Path

WORKSPACE = base64.b64decode({workspace_b64!r}).decode()
STAGE2_RUNS = base64.b64decode({stage2_b64!r}).decode()
WORKER_RELEASE = base64.b64decode({release_b64!r}).decode()
SENTINEL = base64.b64decode({sentinel_b64!r}).decode()
SUPERVISOR_UUID = {SUPERVISOR_UUID!r}
WORKER_UUID = {SOURCE_WORKER_UUID!r}
AUDITOR_UUID = {SOURCE_AUDITOR_UUID!r}

def option(name):
    index = sys.argv.index(name)
    return sys.argv[index + 1]

def direct_read(target):
    try:
        if os.path.isdir(target):
            os.listdir(target)
            return False
        with open(target, "rb") as handle:
            handle.read(1)
        return False
    except OSError:
        return True

def supervisor(blind_stdin):
    home = Path("/home/supervisor")
    auth_object = json.loads(
        (home / "auth.json").read_text(encoding="utf-8")
    )
    release = home / "probe-release"
    deadline = time.monotonic() + 15
    while not release.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    turn_path = home / "turn-count"
    turn = int(turn_path.read_text(encoding="ascii")) + 1 if turn_path.exists() else 1
    turn_path.write_text(str(turn), encoding="ascii")
    session_path = home / "session-id"
    if turn == 1:
        session_path.write_text(SUPERVISOR_UUID, encoding="ascii")
    session_ok = session_path.read_text(encoding="ascii") == SUPERVISOR_UUID
    resumed = "resume" in sys.argv
    exact_resume = (not resumed and turn == 1) or (
        resumed and sys.argv[sys.argv.index("resume") + 1] == SUPERVISOR_UUID
    )
    root_found = False
    for current, directories, files in os.walk("/", followlinks=False):
        directories[:] = [
            name for name in directories
            if str(Path(current) / name) not in {{"/proc", "/dev"}}
        ]
        if SENTINEL in files or SENTINEL in directories:
            root_found = True
            break
    inherited = []
    for item in Path("/proc/self/fd").iterdir():
        try:
            number = int(item.name)
            target = os.readlink(item)
        except (OSError, ValueError):
            continue
        if number > 2:
            inherited.append({{"fd": number, "target": target}})
    try:
        home_entries = sorted(item.name for item in Path("/home").iterdir())
    except OSError:
        home_entries = ["<unreadable>"]
    try:
        mnt_entries = sorted(item.name for item in Path("/mnt").iterdir())
    except OSError:
        mnt_entries = []
    try:
        Path("/workspace/write-probe").write_text("forbidden")
        workspace_read_only = False
    except OSError:
        workspace_read_only = True
    try:
        Path("/home/supervisor/auth.json").write_text("forbidden")
        auth_read_only = False
    except OSError:
        auth_read_only = True
    report = {{
        "turn": turn,
        "session_ok": session_ok,
        "exact_resume": exact_resume,
        "workspace_absent": not os.path.exists(WORKSPACE),
        "workspace_read_denied": direct_read(WORKSPACE),
        "stage2_absent": not os.path.exists(STAGE2_RUNS),
        "stage2_read_denied": direct_read(STAGE2_RUNS),
        "proc_self_denied": direct_read("/proc/self/root" + WORKSPACE),
        "proc_1_denied": direct_read("/proc/1/root" + WORKSPACE),
        "proc_self_root": os.readlink("/proc/self/root"),
        "proc_1_root": os.readlink("/proc/1/root"),
        "home_entries": home_entries,
        "mnt_entries": mnt_entries,
        "future_sentinel_found": root_found,
        "stdin_future_sentinel_absent": SENTINEL.encode() not in blind_stdin,
        "stdin_authoritative_workspace_absent": WORKSPACE.encode() not in blind_stdin,
        "stdin_stage2_locator_absent": STAGE2_RUNS.encode() not in blind_stdin,
        "unexpected_fds": inherited,
        "workspace_read_only": workspace_read_only,
        "schema_readable": Path("/control/output-schema.json").is_file(),
        "auth_readable": Path("/home/supervisor/auth.json").is_file(),
        "auth_used": bool(auth_object.get("token")),
        "auth_read_only": auth_read_only,
        "home": os.environ.get("HOME"),
        "codex_home": os.environ.get("CODEX_HOME"),
        "tmpdir": os.environ.get("TMPDIR"),
    }}
    (home / f"probe-{{turn}}.json").write_text(
        json.dumps(report, sort_keys=True), encoding="utf-8"
    )
    proposal = {worker_proposal!r} if turn == 1 else {auditor_proposal!r}
    Path(option("--output-last-message")).write_text(proposal, encoding="utf-8")
    print(json.dumps({{"type": "thread.started", "thread_id": SUPERVISOR_UUID}}))
    return 0

def main():
    if sys.argv[1:] == ["--version"]:
        print("codex-cli 0.200.0")
        return 0
    sandbox = option("--sandbox")
    ephemeral = "--ephemeral" in sys.argv
    if sandbox == "read-only" and not ephemeral:
        blind_stdin = sys.stdin.buffer.read()
        return supervisor(blind_stdin)
    if sandbox == "workspace-write":
        Path({str(worker_release.parent / "worker-ready")!r}).write_text("ready")
        deadline = time.monotonic() + 15
        while not Path(WORKER_RELEASE).is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        final = {worker_final!r}
        thread = WORKER_UUID
    else:
        final = {auditor_final!r}
        thread = AUDITOR_UUID
    sys.stdin.buffer.read()
    Path(option("--output-last-message")).write_text(final, encoding="utf-8")
    print(json.dumps({{"type": "thread.started", "thread_id": thread}}))
    return 0

raise SystemExit(main())
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_real_bubblewrap_denies_authority_proc_roots_and_future_sentinel(
    tmp_path: Path,
) -> None:
    if not Path("/usr/bin/bwrap").is_file():
        pytest.skip("Bubblewrap executable genuinely does not exist")
    user_namespace_probe = subprocess.run(
        [
            "/usr/bin/bwrap",
            "--unshare-user",
            "--unshare-pid",
            "--proc",
            "/proc",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--",
            "/usr/bin/true",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        close_fds=True,
    )
    if user_namespace_probe.returncode != 0:
        pytest.skip("Bubblewrap user namespaces are genuinely unavailable")
    spec, _, project, _, base_services = create_live_shadow_tree(tmp_path)
    (project / ".gitignore").write_text(".future-sentinel-*\n", encoding="ascii")
    git(project, "add", ".gitignore")
    git(project, "commit", "-q", "-m", "ignore isolation sentinel")
    future_sentinel = f".future-sentinel-{hashlib.sha256(os.urandom(32)).hexdigest()}"
    worker_release = tmp_path / "coordination" / "worker-release"
    worker_release.parent.mkdir()
    fake = tmp_path / "adversarial-fake-codex"
    _write_isolation_fake(
        fake,
        authoritative_workspace=project,
        authoritative_stage2_runs=tmp_path / "stage2-runs",
        worker_release=worker_release,
        future_sentinel=future_sentinel,
    )
    auth_secret = f"AUTH-MATERIAL-{hashlib.sha256(os.urandom(32)).hexdigest()}"
    auth = tmp_path / "subscription-auth.json"
    auth.write_text(json.dumps({"token": auth_secret}), encoding="ascii")
    environment = dict(os.environ)
    environment.pop("FAKE_CODEX_CONFIG", None)
    services = replace(
        base_services,
        codex_executable=str(fake),
        supervisor_invoker=None,
        isolation_preflight=preflight_bubblewrap_isolation,
        bubblewrap_executable="/usr/bin/bwrap",
        codex_authentication_file=auth,
        environ=environment,
    )
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = run_live_shadow(
                spec,
                runs_dir=tmp_path / "live-runs",
                stage2_runs_dir=tmp_path / "stage2-runs",
                services=services,
            )
        except BaseException as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    run_directory: Path | None = None
    stage2_run: Path | None = None
    while time.monotonic() < deadline:
        candidates = tuple((tmp_path / "live-runs").glob("*"))
        if candidates:
            run_directory = candidates[0]
            journal = run_directory / "journal.jsonl"
            run_record = run_directory / "authoritative" / "stage2-run.json"
            if journal.is_file() and run_record.is_file():
                reasons = {
                    json.loads(line)["reason"]
                    for line in journal.read_text(encoding="ascii").splitlines()
                }
                if "live_decision_envelope_frozen" in reasons:
                    stage2_run = Path(
                        json.loads(run_record.read_text(encoding="utf-8"))[
                            "run_directory"
                        ]
                    )
                    break
        time.sleep(0.01)
    assert run_directory is not None
    assert stage2_run is not None

    (project / future_sentinel).write_text(
        future_sentinel,
        encoding="ascii",
    )
    runtime_home = run_directory / "quarantine" / "codex-home"
    (runtime_home / "probe-release").write_text("release", encoding="ascii")
    worker_release.write_text("release", encoding="ascii")
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert "error" not in outcome, repr(outcome.get("error"))
    result = outcome["result"]
    assert isinstance(result, LiveShadowResult)
    assert result.status == "awaiting_reviews"
    assert result.supervisor_session_id == SUPERVISOR_UUID

    reports = [
        json.loads((runtime_home / f"probe-{turn}.json").read_text(encoding="utf-8"))
        for turn in (1, 2)
    ]
    for turn, report in enumerate(reports, start=1):
        assert report["turn"] == turn
        assert report["session_ok"] is True
        assert report["exact_resume"] is True
        assert report["workspace_absent"] is True
        assert report["workspace_read_denied"] is True
        assert report["stage2_absent"] is True
        assert report["stage2_read_denied"] is True
        assert report["proc_self_denied"] is True
        assert report["proc_1_denied"] is True
        assert report["proc_self_root"] == "/"
        assert report["proc_1_root"] == "/"
        assert report["home_entries"] == ["supervisor"]
        assert report["mnt_entries"] == []
        assert report["future_sentinel_found"] is False
        assert report["stdin_future_sentinel_absent"] is True
        assert report["stdin_authoritative_workspace_absent"] is True
        assert report["stdin_stage2_locator_absent"] is True
        assert report["unexpected_fds"] == []
        assert report["workspace_read_only"] is True
        assert report["schema_readable"] is True
        assert report["auth_readable"] is True
        assert report["auth_used"] is True
        assert report["auth_read_only"] is True
        assert report["home"] == ISOLATED_HOME
        assert report["codex_home"] == ISOLATED_HOME
        assert report["tmpdir"] == ISOLATED_TMPDIR

    second_metadata = json.loads(
        (
            run_directory
            / "proposals/auditor-r000-a002/stage1-run/metadata.json"
        ).read_text(encoding="utf-8")
    )
    command = second_metadata["command"]
    resume_index = command.index("resume")
    assert command[resume_index + 1] == SUPERVISOR_UUID
    assert command[0] == "/usr/bin/bwrap"
    assert "--last" not in command
    assert "--all" not in command

    for index, proposal_id in enumerate(
        ("worker_initial-r000-a001", "auditor-r000-a002"),
        start=1,
    ):
        record_live_shadow_review(
            run_directory,
            proposal_id,
            write_review(
                tmp_path / f"integration-review-{index}.yaml",
                proposal_id,
            ),
            services=services,
        )
    report_bytes = json.dumps(
        live_shadow_report(run_directory),
        sort_keys=True,
    ).encode("utf-8")

    sentinel_bytes = future_sentinel.encode("utf-8")
    auth_bytes = auth_secret.encode("utf-8")
    assert sentinel_bytes not in report_bytes
    assert auth_bytes not in report_bytes
    for artifact in run_directory.rglob("*"):
        if artifact.is_file():
            content = artifact.read_bytes()
            assert sentinel_bytes not in content, artifact
            assert auth_bytes not in content, artifact


def _write_auth_exfiltration_fake(path: Path) -> None:
    worker_final = worker_result()
    auditor_final = auditor_result()
    source = f"""#!/usr/bin/python3
import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import quote, quote_plus

WORKER_UUID = {SOURCE_WORKER_UUID!r}
AUDITOR_UUID = {SOURCE_AUDITOR_UUID!r}
SUPERVISOR_UUID = {SUPERVISOR_UUID!r}

def option(name):
    index = sys.argv.index(name)
    return sys.argv[index + 1]

def main():
    if sys.argv[1:] == ["--version"]:
        print("codex-cli 0.200.0")
        return 0
    sandbox = option("--sandbox")
    ephemeral = "--ephemeral" in sys.argv
    sys.stdin.buffer.read()
    if sandbox == "read-only" and not ephemeral:
        auth_text = Path("/home/supervisor/auth.json").read_text(encoding="utf-8")
        token = json.loads(auth_text)["access_token"]
        raw = token.encode()
        forms = [
            token,
            json.dumps(token)[1:-1],
            base64.b64encode(raw).decode(),
            base64.urlsafe_b64encode(raw).decode(),
            raw.hex(),
            "Bearer " + token,
            quote(token, safe=""),
            quote_plus(token, safe=""),
        ]
        home = Path("/home/supervisor")
        count_path = home / "supervisor-launch-count"
        count = int(count_path.read_text()) + 1 if count_path.exists() else 1
        count_path.write_text(str(count), encoding="ascii")
        (home / "auth-read-ok").write_text("yes", encoding="ascii")
        (home / "copied-auth.txt").write_text(forms[2], encoding="utf-8")
        for index, form in enumerate(forms):
            target = home / "path-leaks" / str(index) / form
            target.parent.mkdir(parents=True, exist_ok=True)
            if index % 2:
                target.mkdir(parents=True, exist_ok=True)
                (target / "safe").write_text("safe", encoding="ascii")
            else:
                target.write_text("safe", encoding="ascii")
        raw_name = (
            os.fsencode("/home/supervisor")
            + b"/nonutf-\\xff-"
            + raw.hex().encode("ascii")
        )
        with open(raw_name, "wb") as handle:
            handle.write(b"safe")
        Path("/action/arbitrary-copy.bin").write_bytes(
            ("\\n".join(forms)).encode("utf-8")
        )
        Path(option("--output-last-message")).write_text(
            json.dumps({{"proposal_kind": "worker_initial", "prompt": forms[5]}}),
            encoding="utf-8",
        )
        print(json.dumps({{"type": "thread.started", "thread_id": SUPERVISOR_UUID}}))
        print(json.dumps({{"type": "auth.copy", "value": forms[0]}}))
        print(forms[3])
        print(forms[4], file=sys.stderr)
        return 0
    if sandbox == "workspace-write":
        final = {worker_final!r}
        thread = WORKER_UUID
    else:
        final = {auditor_final!r}
        thread = AUDITOR_UUID
    Path(option("--output-last-message")).write_text(final, encoding="utf-8")
    print(json.dumps({{"type": "thread.started", "thread_id": thread}}))
    return 0

raise SystemExit(main())
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_real_bubblewrap_rejects_and_scrubs_authentication_exfiltration(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if not Path("/usr/bin/bwrap").is_file():
        pytest.skip("Bubblewrap executable genuinely does not exist")
    probe = subprocess.run(
        [
            "/usr/bin/bwrap",
            "--unshare-user",
            "--unshare-pid",
            "--proc",
            "/proc",
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            "/lib",
            "/lib",
            "--ro-bind",
            "/lib64",
            "/lib64",
            "--",
            "/usr/bin/true",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        close_fds=True,
    )
    if probe.returncode != 0:
        pytest.skip("Bubblewrap user namespaces are genuinely unavailable")
    spec, _, _, _, base_services = create_live_shadow_tree(tmp_path)
    fake = tmp_path / "auth-exfiltration-fake-codex"
    _write_auth_exfiltration_fake(fake)
    unique = hashlib.sha256(os.urandom(32)).hexdigest()
    auth_secret = f'AUTH-{unique}/+\\"END'
    auth = tmp_path / "subscription-auth.json"
    auth.write_text(
        json.dumps(
            {
                "auth_mode": "subscription",
                "provider": "openai",
                "access_token": auth_secret,
                "expires_at": "2099-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    services = replace(
        base_services,
        codex_executable=str(fake),
        supervisor_invoker=None,
        isolation_preflight=preflight_bubblewrap_isolation,
        bubblewrap_executable="/usr/bin/bwrap",
        codex_authentication_file=auth,
        environ={
            key: value
            for key, value in os.environ.items()
            if key != "FAKE_CODEX_CONFIG"
        },
    )
    result = run_live_shadow(
        spec,
        runs_dir=tmp_path / "live-runs",
        stage2_runs_dir=tmp_path / "stage2-runs",
        services=services,
    )
    assert result.status == "shadow_degraded"
    assert result.authoritative_stage2_status == "completed"
    assert result.auth_confidentiality_violation_detected is True
    assert result.supervisor_session_usable is False
    assert result.proposal_count == 2
    run_directory = Path(result.artifact_directory)
    state = json.loads(
        (run_directory / "state.json").read_text(encoding="utf-8")
    )
    assert any(
        failure["reason"] == "auth_confidentiality_violation"
        for failure in state["shadow_failures"]
    )
    runtime_home = run_directory / "quarantine" / "codex-home"
    assert not (runtime_home / "copied-auth.txt").exists()
    assert not (runtime_home / "supervisor-launch-count").exists()
    assert not tuple(runtime_home.iterdir())
    assert state["runtime_confidentiality_violation_intent_recorded"] is True
    assert state["runtime_home_cleanup_completed"] is True
    assert not (
        run_directory
        / "proposals/auditor-r000-a002/stage1-run/stage2-completion.json"
    ).exists()
    first_metadata = json.loads(
        (
            run_directory
            / "proposals/worker_initial-r000-a001/stage1-run/metadata.json"
        ).read_text(encoding="utf-8")
    )
    assert first_metadata["confidentiality_violation_detected"] is True
    assert "<AUTHENTICATION_FILE>" in first_metadata["command"]
    assert str(auth) not in first_metadata["command"]
    raw = auth_secret.encode("utf-8")
    protected = {
        raw,
        json.dumps(auth_secret)[1:-1].encode("utf-8"),
        base64.b64encode(raw),
        base64.urlsafe_b64encode(raw),
        raw.hex().encode("ascii"),
        raw.hex().upper().encode("ascii"),
        f"Bearer {auth_secret}".encode(),
        quote(auth_secret, safe="").encode("ascii"),
        quote_plus(auth_secret, safe="").encode("ascii"),
    }
    report_bytes = json.dumps(
        live_shadow_report(run_directory, services=services),
        sort_keys=True,
    ).encode("utf-8")
    captured = capsys.readouterr()
    captured_bytes = (
        captured.out + captured.err
    ).encode("utf-8", errors="replace")
    for fragment in protected:
        assert fragment not in report_bytes
        assert fragment not in captured_bytes
        assert fragment not in _raw_runtime_tree(run_directory)
        for artifact in run_directory.rglob("*"):
            if artifact.is_file():
                assert fragment not in artifact.read_bytes(), artifact
