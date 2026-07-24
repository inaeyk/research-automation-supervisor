"""Nonpersisted authentication-secret derivation and byte scanning."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

from research_automation_supervisor.errors import (
    LiveShadowDependencyError,
    LiveShadowIntegrityError,
)

MAX_AUTHENTICATION_BYTES = 256 * 1024
MIN_PROTECTED_VALUE_BYTES = 8

_JWT = re.compile(
    r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$"
)
_BEARER = re.compile(r"(?i)^Bearer[ \t]+(.+)$")
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "access",
        "apikey",
        "authorization",
        "bearer",
        "cookie",
        "credential",
        "identity",
        "refreshtoken",
        "secret",
        "session",
        "token",
    }
)


@dataclass(frozen=True)
class AuthenticationConfidentiality:
    """Safe public facts plus non-represented, process-memory-only fragments."""

    enabled: bool
    protected_logical_value_count: int
    scan_completed: bool
    _fragments: tuple[str, ...] = field(repr=False, compare=False)
    _byte_fragments: tuple[bytes, ...] = field(repr=False, compare=False)

    def text_fragments(self) -> tuple[str, ...]:
        """Return fragments only to the in-process redaction boundary."""
        return self._fragments

    def contains_bytes(self, value: bytes) -> bool:
        """Whether raw bytes contain a protected value or encoding."""
        return any(fragment in value for fragment in self._byte_fragments)

    def redact_bytes(self, value: bytes) -> bytes:
        """Redact protected byte literals without decoding untrusted output."""
        redacted = value
        for fragment in self._byte_fragments:
            redacted = redacted.replace(fragment, b"<REDACTED>")
        return redacted


def load_authentication_confidentiality(
    authentication_file: Path,
    *,
    forbidden_roots: Sequence[Path],
) -> AuthenticationConfidentiality:
    """Read and derive auth fragments with no-follow regular-file checks."""
    path = _hardened_authentication_path(
        authentication_file,
        forbidden_roots=forbidden_roots,
    )
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > MAX_AUTHENTICATION_BYTES
        ):
            raise LiveShadowDependencyError(
                "Codex subscription authentication is unavailable"
            )
        raw = bytearray()
        while len(raw) <= MAX_AUTHENTICATION_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_AUTHENTICATION_BYTES + 1 - len(raw)),
            )
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if (
            len(raw) > MAX_AUTHENTICATION_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            raise LiveShadowDependencyError(
                "Codex subscription authentication is unavailable"
            )
    except LiveShadowDependencyError:
        raise
    except OSError:
        raise LiveShadowDependencyError(
            "Codex subscription authentication is unavailable"
        ) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    try:
        text = bytes(raw).decode("utf-8")
        parsed = json.loads(text, parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise LiveShadowDependencyError(
            "Codex subscription authentication is unavailable"
        ) from None
    if not isinstance(parsed, Mapping):
        raise LiveShadowDependencyError(
            "Codex subscription authentication is unavailable"
        )

    logical_values: set[str] = {text}
    _collect_sensitive_values(parsed, logical_values, protected=False)
    fragments: set[str] = set()
    for logical_value in logical_values:
        _add_representations(logical_value, fragments)
    ordered = tuple(sorted(fragments, key=lambda item: (-len(item), item)))
    byte_fragments = tuple(
        sorted(
            {item.encode("utf-8") for item in ordered},
            key=lambda item: (-len(item), item),
        )
    )
    return AuthenticationConfidentiality(
        enabled=True,
        protected_logical_value_count=len(logical_values),
        scan_completed=True,
        _fragments=ordered,
        _byte_fragments=byte_fragments,
    )


def _hardened_authentication_path(
    path: Path,
    *,
    forbidden_roots: Sequence[Path],
) -> Path:
    if ".." in path.parts:
        raise LiveShadowDependencyError(
            "Codex subscription authentication is unavailable"
        )
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        status = path.lstat()
    except (OSError, RuntimeError, ValueError):
        raise LiveShadowDependencyError(
            "Codex subscription authentication is unavailable"
        ) from None
    if (
        absolute != resolved
        or path.is_symlink()
        or not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
    ):
        raise LiveShadowDependencyError(
            "Codex subscription authentication is unavailable"
        )
    for root in forbidden_roots:
        try:
            forbidden = root.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as exc:
            raise LiveShadowIntegrityError(
                "authentication forbidden-root validation failed"
            ) from exc
        if _paths_overlap(resolved, forbidden):
            raise LiveShadowDependencyError(
                "Codex subscription authentication is unavailable"
            )
    return resolved


def _collect_sensitive_values(
    value: object,
    logical_values: set[str],
    *,
    protected: bool,
) -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                raise LiveShadowDependencyError(
                    "Codex subscription authentication is unavailable"
                )
            _collect_sensitive_values(
                item,
                logical_values,
                protected=protected or _credential_bearing_key(raw_key),
            )
        return
    if isinstance(value, list):
        for item in value:
            _collect_sensitive_values(
                item,
                logical_values,
                protected=protected,
            )
        return
    if isinstance(value, str):
        bearer = _BEARER.fullmatch(value)
        is_credential = protected or bearer is not None or _JWT.fullmatch(value) is not None
        if not is_credential:
            return
        candidate = bearer.group(1) if bearer is not None else value
        if (
            not candidate
            or len(candidate.encode("utf-8")) < MIN_PROTECTED_VALUE_BYTES
        ):
            raise LiveShadowDependencyError(
                "Codex subscription authentication is unavailable"
            )
        logical_values.add(candidate)
        if bearer is not None:
            logical_values.add(value)
        return
    if protected and value is not None:
        raise LiveShadowDependencyError(
            "Codex subscription authentication is unavailable"
        )


def _credential_bearing_key(key: str) -> bool:
    normalized = "".join(character for character in key.casefold() if character.isalnum())
    return (
        normalized in _SENSITIVE_KEY_PARTS
        or any(
            normalized.endswith(part)
            for part in (
                "apikey",
                "authorization",
                "bearertoken",
                "cookie",
                "credential",
                "identitytoken",
                "refreshtoken",
                "secret",
                "sessionid",
                "sessiontoken",
                "token",
            )
        )
    )


def _add_representations(value: str, fragments: set[str]) -> None:
    raw = value.encode("utf-8")
    representations = {
        value,
        json.dumps(value, ensure_ascii=True)[1:-1],
        json.dumps(value, ensure_ascii=False)[1:-1],
        quote(value, safe=""),
        quote_plus(value, safe=""),
        raw.hex(),
        raw.hex().upper(),
        base64.b64encode(raw).decode("ascii"),
        base64.urlsafe_b64encode(raw).decode("ascii"),
        f"Bearer {value}",
        f"bearer {value}",
    }
    for encoded in tuple(representations):
        if encoded.endswith("="):
            representations.add(encoded.rstrip("="))
    fragments.update(item for item in representations if item)


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _reject_json_constant(value: str) -> Any:
    del value
    raise ValueError("non-standard JSON constant")
