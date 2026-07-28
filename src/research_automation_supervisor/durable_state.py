"""Qualified generic durability primitives shared by workflow engines."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import BaseModel, ValidationError

ModelT = TypeVar("ModelT", bound=BaseModel)
ErrorFactory = Callable[[str], Exception]

ZERO_HASH = "0" * 64


def canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def render_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def atomic_write_bytes(
    path: Path,
    value: bytes,
    *,
    error_factory: ErrorFactory,
    error_message: str,
    fsync_directory_callback: Callable[[Path], None] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        (fsync_directory_callback or fsync_directory)(path.parent)
    except OSError as exc:
        raise error_factory(error_message) from exc
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def atomic_write_json(
    path: Path,
    value: object,
    *,
    error_factory: ErrorFactory,
    error_message: str,
    fsync_directory_callback: Callable[[Path], None] | None = None,
) -> None:
    atomic_write_bytes(
        path,
        render_json_bytes(value),
        error_factory=error_factory,
        error_message=error_message,
        fsync_directory_callback=fsync_directory_callback,
    )


def append_hashed_journal_entry(
    journal: Path,
    body: Mapping[str, object],
    *,
    validate: Callable[[Mapping[str, object]], None],
    error_factory: ErrorFactory,
    error_message: str,
    fsync_directory_callback: Callable[[Path], None] | None = None,
) -> tuple[dict[str, object], str]:
    """Validate and durably append one canonical hash-chain entry."""
    entry_hash = hashlib.sha256(canonical_json(body)).hexdigest()
    entry = {**body, "entry_hash": entry_hash}
    validate(entry)
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(journal, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError("journal is not a regular file")
            content = canonical_json(entry)
            offset = 0
            while offset < len(content):
                offset += os.write(descriptor, content[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        (fsync_directory_callback or fsync_directory)(journal.parent)
    except OSError as exc:
        raise error_factory(error_message) from exc
    return entry, entry_hash


def read_hashed_journal(
    journal: Path,
    *,
    error_factory: ErrorFactory,
    malformed_message: str,
) -> list[dict[str, Any]]:
    """Read and verify the generic sequence/previous-hash/hash-chain form."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(journal, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise OSError("journal is not a regular file")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise error_factory(malformed_message) from exc
    previous = ZERO_HASH
    entries: list[dict[str, Any]] = []
    for sequence, raw in enumerate(b"".join(chunks).splitlines(), start=1):
        try:
            value = json.loads(raw.decode("ascii"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise error_factory(malformed_message) from exc
        if not isinstance(value, dict):
            raise error_factory(malformed_message)
        entry = cast(dict[str, Any], value)
        body = {key: item for key, item in entry.items() if key != "entry_hash"}
        digest = hashlib.sha256(canonical_json(body)).hexdigest()
        if (
            entry.get("sequence") != sequence
            or entry.get("previous_hash") != previous
            or entry.get("entry_hash") != digest
        ):
            raise error_factory(malformed_message)
        previous = digest
        entries.append(entry)
    return entries


def reconcile_model_snapshot(
    state: ModelT,
    entries: Sequence[Mapping[str, object]],
    *,
    model: type[ModelT],
    error_factory: ErrorFactory,
    error_message: str,
) -> ModelT:
    """Derive a typed journal-head snapshot from a trusted persisted prefix."""
    typed_state = cast(Any, state)
    sequence = cast(int, typed_state.journal_sequence)
    journal_hash = cast(str, typed_state.journal_hash)
    if sequence > len(entries):
        raise error_factory(error_message)
    if sequence and entries[sequence - 1].get("entry_hash") != journal_hash:
        raise error_factory(error_message)
    current = state
    for entry in entries[sequence:]:
        values = current.model_dump(mode="json")
        updates = entry.get("state_updates")
        if not isinstance(updates, dict):
            raise error_factory(error_message)
        values.update(updates)
        values.update(
            {
                "updated_at": entry.get("timestamp"),
                "journal_sequence": entry.get("sequence"),
                "journal_hash": entry.get("entry_hash"),
            }
        )
        try:
            current = model.model_validate(values)
        except ValidationError as exc:
            raise error_factory(error_message) from exc
    return current


def commit_result_then_state(
    *,
    result_path: Path | None,
    result_value: object | None,
    state_path: Path,
    state_value: object,
    checkpoint: Callable[[str], None],
    error_factory: ErrorFactory,
    error_message: str,
    fsync_directory_callback: Callable[[Path], None] | None = None,
) -> None:
    """Commit a derived public result first and state.json last."""
    if result_path is not None:
        checkpoint("before_result_replacement")
        atomic_write_json(
            result_path,
            result_value,
            error_factory=error_factory,
            error_message=error_message,
            fsync_directory_callback=fsync_directory_callback,
        )
        checkpoint("after_result_replacement")
    checkpoint("before_state_replacement")
    atomic_write_json(
        state_path,
        state_value,
        error_factory=error_factory,
        error_message=error_message,
        fsync_directory_callback=fsync_directory_callback,
    )
    checkpoint("after_state_replacement")


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant: {value}")
