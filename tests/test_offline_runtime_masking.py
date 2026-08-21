from pathlib import Path

import pytest

from research_automation_supervisor.offline_replay_evaluator import (
    OfflineEvaluationError,
    _present_masked_runtime_directories,
)


def test_absent_masked_runtime_directory_is_allowed(tmp_path: Path) -> None:
    present = tmp_path / "present"
    present.mkdir()
    absent = tmp_path / "absent"

    assert _present_masked_runtime_directories((absent, present)) == (present,)


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling_symlink"])
def test_invalid_masked_runtime_path_fails_closed(
    tmp_path: Path,
    kind: str,
) -> None:
    target = tmp_path / "target"

    if kind == "file":
        target.write_text("not a directory\n", encoding="utf-8")
    elif kind == "symlink":
        real = tmp_path / "real"
        real.mkdir()
        target.symlink_to(real, target_is_directory=True)
    else:
        target.symlink_to(tmp_path / "missing", target_is_directory=True)

    with pytest.raises(
        OfflineEvaluationError,
        match="audited unrelated runtime directory is invalid",
    ):
        _present_masked_runtime_directories((target,))
