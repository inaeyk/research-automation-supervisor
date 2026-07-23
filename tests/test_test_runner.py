from __future__ import annotations

import sys
from pathlib import Path

from research_automation_supervisor.test_runner import run_test_attempt, run_test_suite
from research_automation_supervisor.workflow_models import PreparedWorkflowTest, WorkflowTest


def prepared(
    tmp_path: Path,
    test_id: str,
    source: str,
    *,
    timeout: int = 5,
    stdout_limit: int = 4096,
) -> PreparedWorkflowTest:
    script = tmp_path / f"{test_id}.py"
    script.write_text(source, encoding="utf-8")
    return PreparedWorkflowTest(
        specification=WorkflowTest(
            id=test_id,
            argv=(sys.executable, str(script)),
            cwd=str(tmp_path),
            timeout_seconds=timeout,
            max_stdout_bytes=stdout_limit,
            max_stderr_bytes=4096,
        ),
        cwd=tmp_path,
    )


def test_exact_argv_cwd_environment_filtering_and_redacted_logs(tmp_path: Path) -> None:
    test = prepared(
        tmp_path,
        "pass",
        "import os\nprint(os.getcwd())\nprint(os.environ.get('DEMO_TOKEN', 'removed'))\n",
    )

    result = run_test_attempt(
        test,
        tmp_path / "artifacts",
        "test-pass",
        environ={"PATH": "/usr/bin", "DEMO_TOKEN": "SECRET_VALUE"},
    )

    assert result.passed
    assert result.argv == test.specification.argv
    assert result.cwd == str(tmp_path)
    assert result.removed_environment_variable_names == ("DEMO_TOKEN",)
    log = Path(result.stdout_artifact or "").read_text(encoding="utf-8")
    assert "SECRET_VALUE" not in log
    assert "removed" in log


def test_nonzero_timeout_output_limit_and_invalid_bytes_are_normalized(tmp_path: Path) -> None:
    failed = prepared(tmp_path, "failed", "raise SystemExit(4)\n")
    timeout = prepared(tmp_path, "timeout", "import time\ntime.sleep(5)\n", timeout=1)
    limited = prepared(tmp_path, "limited", "print('x' * 5000)\n", stdout_limit=16)
    invalid = prepared(
        tmp_path,
        "invalid",
        "import sys\nsys.stdout.buffer.write(b'\\xff')\n",
    )

    assert run_test_attempt(failed, tmp_path / "a", "failed").status == "failed"
    assert run_test_attempt(timeout, tmp_path / "b", "timeout").status == "timed_out"
    assert (
        run_test_attempt(limited, tmp_path / "c", "limited").status
        == "output_limit_exceeded"
    )
    invalid_result = run_test_attempt(invalid, tmp_path / "d", "invalid")
    assert invalid_result.passed
    assert "�" in Path(invalid_result.stdout_artifact or "").read_text(encoding="utf-8")


def test_suite_stops_after_first_failure_and_records_skips(tmp_path: Path) -> None:
    first = prepared(tmp_path, "first", "raise SystemExit(2)\n")
    marker = tmp_path / "ran"
    second = prepared(
        tmp_path,
        "second",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
    )

    suite = run_test_suite((first, second), tmp_path / "suite")

    assert not suite.passed
    assert [result.status for result in suite.results] == ["failed", "skipped"]
    assert not marker.exists()


def test_launch_failure_is_normalized_without_a_retry(tmp_path: Path) -> None:
    missing = PreparedWorkflowTest(
        specification=WorkflowTest(
            id="missing",
            argv=(str(tmp_path / "does-not-exist"),),
            cwd=str(tmp_path),
            timeout_seconds=1,
            max_stdout_bytes=64,
            max_stderr_bytes=64,
        ),
        cwd=tmp_path,
    )

    result = run_test_attempt(missing, tmp_path / "missing-artifacts", "missing")

    assert result.status == "launch_failed"
    assert result.exit_code is None
    assert not result.passed
