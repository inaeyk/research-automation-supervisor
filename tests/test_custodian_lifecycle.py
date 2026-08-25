from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest

from research_automation_supervisor.custodian_lifecycle import (
    CustodianLifecycleError,
    custodian_service_identity,
    replace_custodian_service,
)


class _ScriptedRunner:
    def __init__(self, *results: tuple[int, str]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(arguments))
        returncode, stdout = self.results.pop(0)
        return subprocess.CompletedProcess(arguments, returncode, stdout, "")


def _paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    data_root = tmp_path / "application-data"
    state = data_root / "custodian-state"
    codex_home = data_root / "codex-home"
    working = tmp_path / "working"
    for path in (state, codex_home, working):
        path.mkdir(parents=True, exist_ok=True)
    return data_root, working, state / "backend.log", codex_home


def test_new_custodian_uses_detached_user_service_with_bounded_group_cleanup(
    tmp_path: Path,
) -> None:
    data_root, working, backend_log, codex_home = _paths(tmp_path)
    runner = _ScriptedRunner((0, "LoadState=not-found\nActiveState=inactive\n"), (0, ""))

    identity = replace_custodian_service(
        data_root=data_root,
        working_directory=working,
        backend_log=backend_log,
        codex_home=codex_home,
        qualified_commit="a" * 64,
        command=(sys.executable, "-c", "raise SystemExit(0)"),
        runner=runner,
    )

    launch = runner.calls[-1]
    assert launch[:4] == ("/usr/bin/systemd-run", "--user", "--quiet", "--collect")
    assert f"--unit={identity.unit_name}" in launch
    assert "--property=Type=exec" in launch
    assert "--property=KillMode=control-group" in launch
    assert "--property=KillSignal=SIGTERM" in launch
    assert "--property=FinalKillSignal=SIGKILL" in launch
    assert f"--property=StandardOutput=append:{backend_log}" in launch
    assert f"--property=StandardError=append:{backend_log}" in launch
    assert "nohup" not in launch


def test_normal_relaunch_replaces_only_the_verified_intended_service(tmp_path: Path) -> None:
    data_root, working, backend_log, codex_home = _paths(tmp_path)
    identity = custodian_service_identity(data_root)
    runner = _ScriptedRunner(
        (
            0,
            "LoadState=loaded\nActiveState=active\n"
            f"Description={identity.description}\n",
        ),
        (0, ""),
        (0, "LoadState=not-found\nActiveState=inactive\n"),
        (0, ""),
    )

    replace_custodian_service(
        data_root=data_root,
        working_directory=working,
        backend_log=backend_log,
        codex_home=codex_home,
        qualified_commit="b" * 64,
        command=(sys.executable, "-c", "raise SystemExit(0)"),
        runner=runner,
    )

    assert runner.calls[1] == (
        "/usr/bin/systemctl",
        "--user",
        "stop",
        identity.unit_name,
    )
    assert runner.calls[-1][0] == "/usr/bin/systemd-run"


def test_relaunch_refuses_to_signal_a_same_named_unverified_service(tmp_path: Path) -> None:
    data_root, working, backend_log, codex_home = _paths(tmp_path)
    runner = _ScriptedRunner(
        (0, "LoadState=loaded\nActiveState=active\nDescription=unrelated service\n")
    )

    with pytest.raises(CustodianLifecycleError, match="identity did not match"):
        replace_custodian_service(
            data_root=data_root,
            working_directory=working,
            backend_log=backend_log,
            codex_home=codex_home,
            qualified_commit="c" * 64,
            command=(sys.executable, "-c", "raise SystemExit(0)"),
            runner=runner,
        )

    assert len(runner.calls) == 1


def test_bootstrap_reuses_health_before_replacement_and_never_signals_stale_pid() -> None:
    source = Path("scripts/custodian-bootstrap.sh").read_text(encoding="utf-8")

    assert source.index("if health_matches_any; then") < source.index(
        "research_automation_supervisor.custodian_lifecycle"
    )
    assert "/usr/bin/flock -x 9" in source
    assert "old_pid=" not in source
    assert 'kill -TERM "$old_pid"' not in source
    assert '"/proc/$old_pid/cmdline"' not in source
    assert " nohup " not in source


def _systemd_user_available() -> bool:
    try:
        completed = subprocess.run(
            ["/usr/bin/systemctl", "--user", "show", "--property=Version", "--value"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


@pytest.mark.skipif(not _systemd_user_available(), reason="systemd user manager unavailable")
def test_real_launcher_exit_health_relaunch_and_stale_pid_isolation(tmp_path: Path) -> None:
    data_root, working, backend_log, codex_home = _paths(tmp_path)
    identity = custodian_service_identity(data_root)
    port = _available_port()
    backend = (
        "import http.server,json\n"
        "payload=json.dumps({'ready':True}).encode()\n"
        "class Handler(http.server.BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200); self.send_header('Content-Length',str(len(payload))); "
        "self.end_headers(); self.wfile.write(payload)\n"
        " def log_message(self,*args): pass\n"
        f"http.server.ThreadingHTTPServer(('127.0.0.1',{port}),Handler).serve_forever()\n"
    )
    lifecycle = Path("src/research_automation_supervisor/custodian_lifecycle.py").resolve()
    arguments = [
        sys.executable,
        "-I",
        str(lifecycle),
        "--data-root",
        str(data_root),
        "--working-directory",
        str(working),
        "--backend-log",
        str(backend_log),
        "--codex-home",
        str(codex_home),
        "--qualified-commit",
        "d" * 64,
        "--",
        "/usr/bin/python3",
        "-c",
        backend,
    ]
    sentinel = subprocess.Popen(["/usr/bin/sleep", "60"])
    try:
        (data_root / "custodian-state/backend-readiness.json").write_text(
            json.dumps({"schema_version": 1, "pid": sentinel.pid}) + "\n",
            encoding="utf-8",
        )
        subprocess.run(arguments, check=True, timeout=20)
        _wait_health(port)
        first_pid = _service_pid(identity.unit_name)
        assert first_pid > 1
        assert sentinel.poll() is None

        subprocess.run(arguments, check=True, timeout=20)
        _wait_health(port)
        second_pid = _service_pid(identity.unit_name)
        assert second_pid > 1 and second_pid != first_pid
        assert not Path(f"/proc/{first_pid}").exists()
        assert sentinel.poll() is None
    finally:
        subprocess.run(
            ["/usr/bin/systemctl", "--user", "stop", identity.unit_name],
            check=False,
            capture_output=True,
            timeout=15,
        )
        sentinel.terminate()
        sentinel.wait(timeout=5)


def _available_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_health(port: int) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.5) as response:
                if json.load(response) == {"ready": True}:
                    return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("detached backend did not remain healthy")


def _service_pid(unit_name: str) -> int:
    completed = subprocess.run(
        ["/usr/bin/systemctl", "--user", "show", unit_name, "--property=MainPID", "--value"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return int(completed.stdout.strip())
