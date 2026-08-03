from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from research_automation_supervisor.durable_state import canonical_json
from research_automation_supervisor.errors import (
    PhysicsOracleInputError,
    PhysicsOracleIntegrityError,
)
from research_automation_supervisor.physics_oracle_execution import (
    resume_physics_oracle,
    run_physics_oracle,
    verify_physics_oracle_completion,
)
from research_automation_supervisor.physics_oracle_models import (
    PhysicsOracleCatalogV1,
    PhysicsOracleExecutionResultV1,
    load_physics_oracle_catalog,
)
from research_automation_supervisor.physics_oracle_workspace import (
    collect_physics_oracle_workspace_identity,
)

CONTRACT = Path(__file__).parent / "fixtures/physics/full_contract.yaml"
ORACLE_ID = "background_limit_oracle"
PYTHON = Path("/usr/bin/python3").resolve(strict=True)
BWRAP = Path("/usr/bin/bwrap")


class InjectedCrash(RuntimeError):
    pass


def _git(workspace: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("/usr/bin/git", "-C", str(workspace), *arguments),
        capture_output=True,
        check=True,
        text=True,
    )
    return completed.stdout.strip()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _workspace(tmp_path: Path, source: str) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "oracle.py").write_text(source, encoding="ascii")
    (workspace / "input.txt").write_text("candidate input\n", encoding="ascii")
    _git(workspace, "init", "-q")
    _git(workspace, "config", "user.name", "Synthetic Test")
    _git(workspace, "config", "user.email", "synthetic@example.invalid")
    _git(workspace, "add", "oracle.py", "input.txt")
    _git(workspace, "commit", "-qm", "synthetic baseline")
    return workspace


def _catalog_data(
    workspace: Path,
    *,
    argv: list[str] | None = None,
    timeout: int = 10,
    stdout_limit: int = 65536,
    stderr_limit: int = 65536,
    accepted: list[int] | None = None,
    structured: str = "physics_oracle_result_v1",
    artifacts: list[dict[str, object]] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "catalog_id": "synthetic-catalog",
        "environment_profiles": [
            {
                "schema_version": 1,
                "id": "minimal-python",
                "profile": "minimal_python_v1",
            }
        ],
        "intents": [
            {
                "schema_version": 1,
                "id": ORACLE_ID,
                "executable": {
                    "schema_version": 1,
                    "policy": "isolated_system_python_v1",
                    "path": str(PYTHON),
                    "sha256": _sha256(PYTHON),
                },
                "program": {
                    "path": "oracle.py",
                    "sha256": _sha256(workspace / "oracle.py"),
                },
                "argv": argv or [str(PYTHON), "-I", "-S", "-B", "oracle.py"],
                "execution_policy": {
                    "schema_version": 1,
                    "policy_id": "offline-read-only-v1",
                    "isolation_backend": "bubblewrap_unshare_all_v1",
                    "working_directory": "workspace_root",
                    "workspace_access": "read_only",
                    "scratch_output": "scratch_only",
                    "network": "disabled",
                    "environment_profile_id": "minimal-python",
                    "timeout_seconds": timeout,
                    "max_stdout_bytes": stdout_limit,
                    "max_stderr_bytes": stderr_limit,
                    "accepted_exit_codes": accepted or [0],
                    "structured_output_schema": structured,
                    "required_artifacts": artifacts or [],
                },
            }
        ],
    }


def _catalog(tmp_path: Path, workspace: Path, **kwargs: object) -> Path:
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(_catalog_data(workspace, **kwargs), indent=2, sort_keys=True) + "\n",
        encoding="ascii",
    )
    return path


def _run(
    tmp_path: Path,
    workspace: Path,
    catalog: Path,
    *,
    output_name: str = "oracle-output",
    environ: dict[str, str] | None = None,
    bwrap: Path = BWRAP,
    checkpoint: Any = lambda _name: None,
) -> PhysicsOracleExecutionResultV1:
    return run_physics_oracle(
        catalog_path=catalog,
        contract_path=CONTRACT,
        oracle_id=ORACLE_ID,
        task_id="synthetic-task",
        workspace=workspace,
        output_directory=tmp_path / output_name,
        environ=environ,
        bubblewrap_executable=bwrap,
        checkpoint=checkpoint,
    )


def _passing_source(checks: str = "[]") -> str:
    return (
        "import json\n"
        f"print(json.dumps({{'schema_version': 1, 'oracle_id': {ORACLE_ID!r}, "
        f"'outcome': 'passed', 'checks': {checks}}}, sort_keys=True))\n"
    )


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_fixed_oracle_passes_and_completion_proof_verifies(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, _passing_source())
    before = collect_physics_oracle_workspace_identity(workspace)
    catalog = _catalog(tmp_path, workspace)

    result = _run(tmp_path, workspace, catalog)

    assert result.status == "passed"
    assert result.integrity_verdict == "unchanged"
    assert result.initial_workspace_identity == before
    assert result.final_workspace_identity == before
    assert result.network_enforcement.capability == "enforced"
    assert verify_physics_oracle_completion(tmp_path / "oracle-output") == result


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_declared_functional_failure_is_typed(tmp_path: Path) -> None:
    source = (
        "import json\n"
        f"print(json.dumps({{'schema_version': 1, 'oracle_id': {ORACLE_ID!r}, "
        "'outcome': 'functional_failure', "
        "'checks': [{'id': 'residual', 'passed': False}]}, sort_keys=True))\n"
    )
    workspace = _workspace(tmp_path, source)
    result = _run(tmp_path, workspace, _catalog(tmp_path, workspace))

    assert result.status == "functional_failure"
    assert result.failure_reason == "declared_functional_failure"
    assert result.declared_outcome == "functional_failure"


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_scratch_artifact_is_allowed_and_hashed(tmp_path: Path) -> None:
    source = (
        "import json, os\n"
        "from pathlib import Path\n"
        "Path(os.environ['RAS_ORACLE_SCRATCH'], 'result.bin').write_bytes(b'abc\\x00def')\n"
        + _passing_source()
    )
    workspace = _workspace(tmp_path, source)
    catalog = _catalog(
        tmp_path,
        workspace,
        artifacts=[{"id": "result", "path": "result.bin", "required": True, "max_bytes": 100}],
    )

    result = _run(tmp_path, workspace, catalog)

    assert result.status == "passed"
    assert result.artifacts[0].sha256 == hashlib.sha256(b"abc\x00def").hexdigest()
    assert result.artifacts[0].path == "result.bin"


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_shell_metacharacters_are_literal_arguments(tmp_path: Path) -> None:
    arguments = [";", "&&", "|", "$()", "`literal`", ">", "*.txt"]
    source = (
        "import json, sys\n"
        f"expected = {arguments!r}\n"
        "passed = sys.argv[1:] == expected\n"
        f"print(json.dumps({{'schema_version': 1, 'oracle_id': {ORACLE_ID!r}, "
        "'outcome': 'passed' if passed else 'functional_failure', "
        "'checks': [{'id': 'literal-argv', 'passed': passed}]}, sort_keys=True))\n"
    )
    workspace = _workspace(tmp_path, source)
    argv = [str(PYTHON), "-I", "-S", "-B", "oracle.py", *arguments]

    result = _run(tmp_path, workspace, _catalog(tmp_path, workspace, argv=argv))

    assert result.status == "passed"
    assert not (workspace / "*.txt").exists()


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_trusted_program_bytes_are_sealed_before_launch_to_prevent_toctou(
    tmp_path: Path,
) -> None:
    trusted_source = (
        "import json\n"
        f"print(json.dumps({{'schema_version': 1, 'oracle_id': {ORACLE_ID!r}, "
        "'outcome': 'functional_failure', "
        "'checks': [{'id': 'trusted-program', 'passed': False}]}, sort_keys=True))\n"
    )
    substituted_source = (
        "import json, os\n"
        "from pathlib import Path\n"
        "Path(os.environ['RAS_ORACLE_SCRATCH'], 'substitution-ran').write_text('bad')\n"
        + _passing_source()
    )
    workspace = _workspace(tmp_path, trusted_source)
    catalog = _catalog(tmp_path, workspace)

    def replace_and_restore(name: str) -> None:
        if name == "process_launch_attempted":
            (workspace / "oracle.py").write_text(substituted_source, encoding="ascii")
        elif name == "process_exit_observed":
            (workspace / "oracle.py").write_text(trusted_source, encoding="ascii")

    result = _run(tmp_path, workspace, catalog, checkpoint=replace_and_restore)

    assert result.status == "functional_failure"
    assert result.declared_outcome == "functional_failure"
    assert not (tmp_path / "oracle-output/scratch/substitution-ran").exists()
    assert result.integrity_verdict == "unchanged"


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_child_environment_is_strict_and_credentials_do_not_leak(tmp_path: Path) -> None:
    canary = "SYNTHETIC-CANARY-CREDENTIAL-9f06c1"
    canary_sha256 = hashlib.sha256(canary.encode()).hexdigest()
    source = (
        "import hashlib, json, os\n"
        f"canary_sha256 = {canary_sha256!r}\n"
        "expected = {'HOME', 'LANG', 'LC_ALL', 'PATH', 'PWD', 'PYTHONDONTWRITEBYTECODE', "
        "'PYTHONNOUSERSITE', 'RAS_ORACLE_SCRATCH', 'TMPDIR'}\n"
        "passed = (set(os.environ) == expected "
        "and all(hashlib.sha256(value.encode()).hexdigest() != canary_sha256 "
        "for value in os.environ.values()) "
        "and 'SYNTHETIC_API_TOKEN' not in os.environ)\n"
        f"print(json.dumps({{'schema_version': 1, 'oracle_id': {ORACLE_ID!r}, "
        "'outcome': 'passed' if passed else 'functional_failure', "
        "'checks': [{'id': 'secret-absent', 'passed': passed}]}, sort_keys=True))\n"
    )
    workspace = _workspace(tmp_path, source)
    catalog = _catalog(tmp_path, workspace)
    environment = dict(os.environ)
    environment.update(
        {
            "SYNTHETIC_API_TOKEN": canary,
            "AWS_SECRET_ACCESS_KEY": canary,
            "HTTPS_PROXY": f"http://{canary}@proxy.invalid",
            "SSH_AUTH_SOCK": f"/{canary}",
            "GIT_CONFIG_VALUE_0": canary,
        }
    )

    result = _run(tmp_path, workspace, catalog, environ=environment)

    assert result.status == "passed"
    for path in (tmp_path / "oracle-output").rglob("*"):
        if path.is_file():
            assert canary.encode() not in path.read_bytes()
    assert canary not in repr(result)


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_network_disabled_is_enforced_on_exact_production_path(tmp_path: Path) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    source = (
        "import json, socket\n"
        "connection = socket.socket()\n"
        f"blocked = connection.connect_ex(('127.0.0.1', {port})) != 0\n"
        f"print(json.dumps({{'schema_version': 1, 'oracle_id': {ORACLE_ID!r}, "
        "'outcome': 'passed' if blocked else 'functional_failure', "
        "'checks': [{'id': 'network-blocked', 'passed': blocked}]}, sort_keys=True))\n"
    )
    workspace = _workspace(tmp_path, source)
    try:
        result = _run(tmp_path, workspace, _catalog(tmp_path, workspace))
    finally:
        listener.close()

    assert result.status == "passed"
    assert result.network_enforcement.backend_policy == "unshare_all_network_namespace_v1"


def test_missing_network_capability_fails_closed_without_launch(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, _passing_source())
    result = _run(
        tmp_path,
        workspace,
        _catalog(tmp_path, workspace),
        bwrap=tmp_path / "missing-bwrap",
    )

    assert result.status == "infrastructure_failure"
    assert result.failure_reason == "network_isolation_unavailable"
    assert result.process_exit_code is None
    assert result.network_enforcement.capability == "unavailable"


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_workspace_write_is_kernel_denied_and_mode_0400_input_readable(
    tmp_path: Path,
) -> None:
    source = (
        "import json\n"
        "from pathlib import Path\n"
        "readable = Path('input.txt').read_text() == 'candidate input\\n'\n"
        "try:\n"
        "    Path('input.txt').write_text('mutation')\n"
        "except OSError:\n"
        "    denied = True\n"
        "else:\n"
        "    denied = False\n"
        "passed = readable and denied\n"
        f"print(json.dumps({{'schema_version': 1, 'oracle_id': {ORACLE_ID!r}, "
        "'outcome': 'passed' if passed else 'functional_failure', "
        "'checks': [{'id': 'read-only', 'passed': passed}]}, sort_keys=True))\n"
    )
    workspace = _workspace(tmp_path, source)
    (workspace / "input.txt").chmod(0o400)
    before = collect_physics_oracle_workspace_identity(workspace)

    result = _run(tmp_path, workspace, _catalog(tmp_path, workspace))

    assert result.status == "passed"
    assert stat.S_IMODE((workspace / "input.txt").stat().st_mode) == 0o400
    assert collect_physics_oracle_workspace_identity(workspace) == before


@pytest.mark.parametrize("mutation", ["tracked", "untracked", "mode", "symlink"])
@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_before_after_identity_detects_every_workspace_mutation(
    tmp_path: Path, mutation: str
) -> None:
    workspace = _workspace(tmp_path, _passing_source())
    link = workspace / "link.txt"
    link.symlink_to("input.txt")
    _git(workspace, "add", "link.txt")
    _git(workspace, "commit", "-qm", "add link")
    catalog = _catalog(tmp_path, workspace)

    def checkpoint(name: str) -> None:
        if name != "process_exit_observed":
            return
        if mutation == "tracked":
            (workspace / "input.txt").write_text("changed\n", encoding="ascii")
        elif mutation == "untracked":
            (workspace / "new.txt").write_text("new\n", encoding="ascii")
        elif mutation == "mode":
            (workspace / "input.txt").chmod(0o755)
        else:
            link.unlink()
            link.symlink_to("oracle.py")

    result = _run(tmp_path, workspace, catalog, checkpoint=checkpoint)

    assert result.status == "workspace_integrity_failure"
    assert result.failure_reason == "workspace_changed"
    assert result.integrity_verdict == "changed"


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_timeout_and_output_flood_are_bounded(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "import time\ntime.sleep(60)\n")
    timeout_result = _run(
        tmp_path,
        workspace,
        _catalog(tmp_path, workspace, timeout=1),
        output_name="timeout-output",
    )
    assert timeout_result.status == "timed_out"
    assert timeout_result.timed_out is True

    (workspace / "oracle.py").write_text("print('x' * 1000000)\n", encoding="ascii")
    _git(workspace, "add", "oracle.py")
    _git(workspace, "commit", "-qm", "output flood")
    flood_result = _run(
        tmp_path,
        workspace,
        _catalog(tmp_path, workspace, stdout_limit=1024),
        output_name="flood-output",
    )
    assert flood_result.status == "output_contract_failure"
    assert flood_result.stdout.captured_prefix_byte_length <= 1024
    assert flood_result.stdout.truncated is True


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_scratch_entry_and_undeclared_byte_floods_are_typed_failures(
    tmp_path: Path,
) -> None:
    entry_source = (
        "import os\n"
        "from pathlib import Path\n"
        "scratch = Path(os.environ['RAS_ORACLE_SCRATCH'])\n"
        "for index in range(101): (scratch / f'entry-{index:03d}').write_bytes(b'')\n"
        + _passing_source()
    )
    workspace = _workspace(tmp_path, entry_source)
    with pytest.raises(PhysicsOracleIntegrityError, match="entry count"):
        _run(
            tmp_path,
            workspace,
            _catalog(tmp_path, workspace),
            output_name="entry-flood-output",
        )
    entry_output = tmp_path / "entry-flood-output"
    entry_record = json.loads(
        sorted((entry_output / "action-records").glob("*.json"))[-1].read_text()
    )
    assert entry_record["phase"] == "output_captured"
    assert entry_record["execution_status"] == "output_contract_failure"
    assert entry_record["failure_reason"] == "artifact_contract_failed"
    assert not (entry_output / "completion-proof.json").exists()
    with pytest.raises(PhysicsOracleIntegrityError, match="entry count"):
        resume_physics_oracle(
            catalog_path=tmp_path / "catalog.json",
            contract_path=CONTRACT,
            workspace=workspace,
            output_directory=entry_output,
        )

    large_source = (
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['RAS_ORACLE_SCRATCH'], 'large').write_bytes(b'x' * 2_000_000)\n"
        + _passing_source()
    )
    (workspace / "oracle.py").write_text(large_source, encoding="ascii")
    _git(workspace, "add", "oracle.py")
    _git(workspace, "commit", "-qm", "large undeclared output")
    with pytest.raises(PhysicsOracleIntegrityError, match="undeclared scratch artifact"):
        _run(
            tmp_path,
            workspace,
            _catalog(tmp_path, workspace),
            output_name="large-output",
        )
    large_output = tmp_path / "large-output"
    large_record = json.loads(
        sorted((large_output / "action-records").glob("*.json"))[-1].read_text()
    )
    assert large_record["execution_status"] == "output_contract_failure"
    assert large_record["failure_reason"] == "artifact_contract_failed"
    assert not (large_output / "completion-proof.json").exists()


@pytest.mark.parametrize("kind", ["malformed", "wrong_schema", "wrong_oracle"])
@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_malformed_structured_output_fails_closed(tmp_path: Path, kind: str) -> None:
    if kind == "malformed":
        source = "print('not-json')\n"
    elif kind == "wrong_schema":
        source = "print('{\"schema_version\": 2}')\n"
    else:
        source = (
            "import json\n"
            "print(json.dumps({'schema_version': 1, 'oracle_id': 'different-oracle', "
            "'outcome': 'passed', 'checks': []}))\n"
        )
    workspace = _workspace(tmp_path, source)

    result = _run(tmp_path, workspace, _catalog(tmp_path, workspace))

    assert result.status == "output_contract_failure"
    assert result.failure_reason == "structured_output_malformed"
    assert result.structured_output_status == "malformed"


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_scratch_escape_and_undeclared_output_are_rejected_and_bound(
    tmp_path: Path,
) -> None:
    source = (
        "import json, os\n"
        "from pathlib import Path\n"
        "scratch = Path(os.environ['RAS_ORACLE_SCRATCH'])\n"
        "(scratch / 'escape').symlink_to('/workspace/input.txt')\n"
        "(scratch / 'undeclared').write_text('bounded')\n" + _passing_source()
    )
    workspace = _workspace(tmp_path, source)

    result = _run(tmp_path, workspace, _catalog(tmp_path, workspace))

    assert result.status == "output_contract_failure"
    assert result.failure_reason == "artifact_contract_failed"
    assert {item.kind for item in result.artifacts} == {"regular", "symlink"}
    assert all(not item.declared for item in result.artifacts)
    assert verify_physics_oracle_completion(tmp_path / "oracle-output") == result


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_process_termination_failure_is_infrastructure_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_automation_supervisor import physics_oracle_execution as execution

    workspace = _workspace(tmp_path, "import time\ntime.sleep(60)\n")
    catalog = _catalog(tmp_path, workspace, timeout=1)
    original = execution._terminate_process_group

    def terminate_but_report_unproven(
        process: subprocess.Popen[bytes], grace_seconds: float
    ) -> bool:
        original(process, grace_seconds)
        return False

    monkeypatch.setattr(execution, "_terminate_process_group", terminate_but_report_unproven)
    result = _run(tmp_path, workspace, catalog)

    assert result.status == "infrastructure_failure"
    assert result.failure_reason == "termination_unproven"


@pytest.mark.parametrize(
    "invalid",
    [
        "unknown_field",
        "duplicate_intent",
        "shell_string",
        "unknown_profile",
        "unsupported_network",
        "unbounded_timeout",
        "absolute_artifact",
        "traversal_artifact",
        "unknown_executable",
        "unsupported_schema",
    ],
)
def test_catalog_negative_cases_fail_strictly(tmp_path: Path, invalid: str) -> None:
    workspace = _workspace(tmp_path, _passing_source())
    value = _catalog_data(workspace)
    intent = value["intents"][0]
    policy = intent["execution_policy"]
    if invalid == "unknown_field":
        intent["command"] = "python oracle.py"
    elif invalid == "duplicate_intent":
        value["intents"].append(json.loads(json.dumps(intent)))
    elif invalid == "shell_string":
        intent["argv"] = f"{PYTHON} oracle.py"
    elif invalid == "unknown_profile":
        policy["environment_profile_id"] = "missing"
    elif invalid == "unsupported_network":
        policy["network"] = "best_effort"
    elif invalid == "unbounded_timeout":
        policy["timeout_seconds"] = 1_000_000
    elif invalid == "absolute_artifact":
        policy["required_artifacts"] = [
            {"id": "bad", "path": "/project/out", "required": True, "max_bytes": 1}
        ]
    elif invalid == "traversal_artifact":
        policy["required_artifacts"] = [
            {"id": "bad", "path": "../out", "required": True, "max_bytes": 1}
        ]
    elif invalid == "unknown_executable":
        intent["executable"]["path"] = "/usr/bin/bash"
        intent["argv"][0] = "/usr/bin/bash"
    else:
        value["schema_version"] = 2

    with pytest.raises(ValidationError):
        PhysicsOracleCatalogV1.model_validate(value)


def test_unknown_oracle_and_model_attempt_to_redefine_argv_are_rejected(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, _passing_source())
    catalog = _catalog(tmp_path, workspace)
    with pytest.raises(PhysicsOracleInputError, match="physics contract"):
        run_physics_oracle(
            catalog_path=catalog,
            contract_path=CONTRACT,
            oracle_id="missing-oracle",
            task_id="synthetic-task",
            workspace=workspace,
            output_directory=tmp_path / "missing-output",
        )
    contract_data = json.loads(json.dumps({"schema_version": 1, "argv": ["bad"]}))
    assert "argv" not in {
        name
        for name in __import__(
            "research_automation_supervisor.physics_models", fromlist=["PhysicsOracleV1"]
        ).PhysicsOracleV1.model_fields
    }
    assert contract_data["argv"] == ["bad"]


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_recovery_before_launch_runs_once_and_final_completion_is_reused(
    tmp_path: Path,
) -> None:
    source = (
        "import json, os\n"
        "from pathlib import Path\n"
        "counter = Path(os.environ['RAS_ORACLE_SCRATCH'], 'counter')\n"
        "counter.write_text('once')\n" + _passing_source()
    )
    workspace = _workspace(tmp_path, source)
    catalog = _catalog(
        tmp_path,
        workspace,
        artifacts=[{"id": "counter", "path": "counter", "required": True, "max_bytes": 10}],
    )

    def crash(name: str) -> None:
        if name == "execution_prepared":
            raise InjectedCrash

    with pytest.raises(InjectedCrash):
        _run(tmp_path, workspace, catalog, checkpoint=crash)
    result = resume_physics_oracle(
        catalog_path=catalog,
        contract_path=CONTRACT,
        workspace=workspace,
        output_directory=tmp_path / "oracle-output",
    )
    record_count = len(tuple((tmp_path / "oracle-output/action-records").iterdir()))
    reused = resume_physics_oracle(
        catalog_path=catalog,
        contract_path=CONTRACT,
        workspace=workspace,
        output_directory=tmp_path / "oracle-output",
    )

    assert result.status == "passed"
    assert reused == result
    assert len(tuple((tmp_path / "oracle-output/action-records").iterdir())) == record_count
    assert (tmp_path / "oracle-output/scratch/counter").read_text() == "once"


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_recovery_after_intent_persistence_is_safe(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, _passing_source())
    catalog = _catalog(tmp_path, workspace)

    def crash(name: str) -> None:
        if name == "intent_accepted":
            raise InjectedCrash

    with pytest.raises(InjectedCrash):
        _run(tmp_path, workspace, catalog, checkpoint=crash)
    result = resume_physics_oracle(
        catalog_path=catalog,
        contract_path=CONTRACT,
        workspace=workspace,
        output_directory=tmp_path / "oracle-output",
    )
    assert result.status == "passed"


@pytest.mark.parametrize(
    "crash_point",
    [
        "process_exit_observed",
        "output_captured",
        "workspace_rechecked",
        "after_proof_creation_before_final_record",
    ],
)
@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_recovery_finalizes_durable_post_exit_boundaries(tmp_path: Path, crash_point: str) -> None:
    workspace = _workspace(tmp_path, _passing_source())
    catalog = _catalog(tmp_path, workspace)

    def crash(name: str) -> None:
        if name == crash_point:
            raise InjectedCrash

    with pytest.raises(InjectedCrash):
        _run(tmp_path, workspace, catalog, checkpoint=crash)
    result = resume_physics_oracle(
        catalog_path=catalog,
        contract_path=CONTRACT,
        workspace=workspace,
        output_directory=tmp_path / "oracle-output",
    )

    assert result.status == "passed"
    assert verify_physics_oracle_completion(tmp_path / "oracle-output") == result


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_ambiguous_launch_recovery_never_reruns(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, "import time\ntime.sleep(60)\n")
    catalog = _catalog(tmp_path, workspace, timeout=120)

    def crash(name: str) -> None:
        if name == "after_process_launch_before_identity":
            raise InjectedCrash

    with pytest.raises(InjectedCrash):
        _run(tmp_path, workspace, catalog, checkpoint=crash)
    running_record = json.loads(
        sorted((tmp_path / "oracle-output/action-records").glob("*.json"))[-1].read_text()
    )
    with pytest.raises(ProcessLookupError):
        os.kill(running_record["process_identity"]["pid"], 0)
    result = resume_physics_oracle(
        catalog_path=catalog,
        contract_path=CONTRACT,
        workspace=workspace,
        output_directory=tmp_path / "oracle-output",
    )

    assert result.status == "indeterminate_recovery"
    assert result.failure_reason == "recovery_ambiguous"


@pytest.mark.parametrize("process_case", ["running", "stale", "reused"])
@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_running_stale_and_reused_process_identity_recovery_is_indeterminate(
    tmp_path: Path, process_case: str
) -> None:
    workspace = _workspace(tmp_path, "import time\ntime.sleep(60)\n")
    catalog = _catalog(tmp_path, workspace, timeout=120)

    def crash(name: str) -> None:
        if name == "process_running":
            raise InjectedCrash

    with pytest.raises(InjectedCrash):
        _run(tmp_path, workspace, catalog, checkpoint=crash)
    record_path = sorted((tmp_path / "oracle-output/action-records").glob("*.json"))[-1]
    record = json.loads(record_path.read_text())
    survivor: subprocess.Popen[bytes] | None = None
    if process_case in {"running", "reused"}:
        survivor = subprocess.Popen(
            ("/usr/bin/sleep", "60"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        stat_fields = Path(f"/proc/{survivor.pid}/stat").read_text().rsplit(")", 1)[1].split()
        record["process_identity"] = {
            "pid": survivor.pid,
            "process_group_id": survivor.pid,
            "start_ticks": int(stat_fields[19]) + (1 if process_case == "reused" else 0),
        }
        payload = {key: value for key, value in record.items() if key != "record_sha256"}
        record["record_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
        record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    try:
        result = resume_physics_oracle(
            catalog_path=catalog,
            contract_path=CONTRACT,
            workspace=workspace,
            output_directory=tmp_path / "oracle-output",
        )
    finally:
        if survivor is not None and survivor.poll() is None:
            os.killpg(survivor.pid, signal.SIGKILL)
        if survivor is not None:
            survivor.wait()

    assert result.status == "indeterminate_recovery"
    assert result.failure_reason == "recovery_ambiguous"


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_resume_rejects_contract_intent_policy_and_workspace_substitution(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, _passing_source())
    catalog = _catalog(tmp_path, workspace)

    def crash(name: str) -> None:
        if name == "intent_accepted":
            raise InjectedCrash

    with pytest.raises(InjectedCrash):
        _run(tmp_path, workspace, catalog, checkpoint=crash)
    value = json.loads(catalog.read_text())
    value["intents"][0]["execution_policy"]["timeout_seconds"] += 1
    catalog.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

    with pytest.raises(PhysicsOracleIntegrityError, match="policy changed"):
        resume_physics_oracle(
            catalog_path=catalog,
            contract_path=CONTRACT,
            workspace=workspace,
            output_directory=tmp_path / "oracle-output",
        )


@pytest.mark.parametrize(
    "target", ["artifact", "result", "proof", "record", "sealed_authority"]
)
@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_tampering_fails_completion_verification(tmp_path: Path, target: str) -> None:
    source = (
        "import json, os\n"
        "from pathlib import Path\n"
        "Path(os.environ['RAS_ORACLE_SCRATCH'], 'artifact').write_text('trusted')\n"
        + _passing_source()
    )
    workspace = _workspace(tmp_path, source)
    catalog = _catalog(
        tmp_path,
        workspace,
        artifacts=[{"id": "artifact", "path": "artifact", "required": True, "max_bytes": 20}],
    )
    _run(tmp_path, workspace, catalog)
    output = tmp_path / "oracle-output"
    if target == "artifact":
        (output / "scratch/artifact").write_text("tampered", encoding="ascii")
    elif target == "result":
        path = output / "result.json"
        value = json.loads(path.read_text())
        value["status"] = "functional_failure"
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    elif target == "proof":
        path = output / "completion-proof.json"
        value = json.loads(path.read_text())
        value["physics_contract_sha256"] = "0" * 64
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    elif target == "record":
        path = sorted((output / "action-records").glob("*.json"))[0]
        value = json.loads(path.read_text())
        value["request"]["trusted_intent_sha256"] = "0" * 64
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    else:
        path = output / "control/trusted-intent.json"
        value = json.loads(path.read_text())
        value["execution_policy"]["timeout_seconds"] += 1
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")

    with pytest.raises(PhysicsOracleIntegrityError):
        verify_physics_oracle_completion(output)


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_final_record_semantics_must_match_the_verified_result(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, _passing_source())
    catalog = _catalog(tmp_path, workspace)
    _run(tmp_path, workspace, catalog)
    output = tmp_path / "oracle-output"
    record_path = sorted((output / "action-records").glob("*.json"))[-1]
    record = json.loads(record_path.read_text())
    record["execution_status"] = "functional_failure"
    record["failure_reason"] = "process_exit_not_accepted"
    payload = {key: value for key, value in record.items() if key != "record_sha256"}
    record["record_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

    with pytest.raises(PhysicsOracleIntegrityError, match="record contradicts"):
        verify_physics_oracle_completion(output)


@pytest.mark.skipif(not BWRAP.is_file(), reason="Bubblewrap production path unavailable")
def test_result_model_rejects_status_reason_and_exit_contradictions(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, _passing_source())
    catalog = _catalog(tmp_path, workspace)
    valid = _run(tmp_path, workspace, catalog).model_dump(mode="json")
    contradictory_values = (
        {**valid, "process_exit_code": None},
        {**valid, "status": "functional_failure", "failure_reason": "none"},
        {
            **valid,
            "status": "output_contract_failure",
            "failure_reason": "declared_functional_failure",
        },
        {**valid, "status": "cancelled", "failure_reason": "recovery_ambiguous"},
    )

    for value in contradictory_values:
        with pytest.raises(ValidationError):
            PhysicsOracleExecutionResultV1.model_validate(value)


def test_catalog_canonicalization_is_collection_order_independent(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, _passing_source())
    value = _catalog_data(workspace)
    second = json.loads(json.dumps(value["intents"][0]))
    second["id"] = "trace_free_oracle"
    value["intents"].append(second)
    forward = PhysicsOracleCatalogV1.model_validate(value)
    value["intents"].reverse()
    reversed_catalog = PhysicsOracleCatalogV1.model_validate(value)

    assert forward.to_canonical_json() == reversed_catalog.to_canonical_json()
    assert forward.canonical_sha256() == reversed_catalog.canonical_sha256()


def test_catalog_file_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    path = tmp_path / "catalog.yaml"
    path.write_text("schema_version: 1\nschema_version: 1\n", encoding="ascii")
    with pytest.raises(PhysicsOracleInputError):
        load_physics_oracle_catalog(path)
