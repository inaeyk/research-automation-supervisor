"""Deterministic text and structured-data redaction for Codex artifacts."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "<REDACTED>"
SENSITIVE_NAME_PARTS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "API_KEY",
    "APIKEY",
    "CREDENTIAL",
    "COOKIE",
    "SESSION",
    "AUTHORIZATION",
)

_AUTHORIZATION_BEARER = re.compile(
    r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s,;]+"
)
_TOKEN_FORM = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:sk-|ghp_|github_pat_|xoxb-|xoxp-)[A-Za-z0-9_.-]+"
)
_ASSIGNED_SECRET = re.compile(
    r"(?ix)"
    r"(?P<prefix>"
    r"(?:[\"']?[^\s\"'=,:;]*(?:token|secret|password|passwd|api_key|apikey|credential|cookie|session)[^\s\"'=,:;]*[\"']?)"
    r"\s*(?:=|:)\s*"
    r")"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_AUTHORIZATION_ASSIGNMENT = re.compile(
    r"(?ix)"
    r"(?P<prefix>[\"']?[^\s\"'=,:;]*authorization[^\s\"'=,:;]*[\"']?\s*(?:=|:)\s*)"
    r"(?!Bearer\s+)"
    r"(?P<value>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)


def is_sensitive_name(name: str) -> bool:
    """Whether a name contains an allowlisted sensitive marker."""
    upper_name = name.upper()
    return any(part in upper_name for part in SENSITIVE_NAME_PARTS)


def redact_text(text: str, sensitive_values: Sequence[str] = ()) -> str:
    """Redact known credential shapes and explicitly supplied secret values."""
    redacted = text
    for value in sorted(
        {value for value in sensitive_values if value and value != REDACTED},
        key=lambda item: (-len(item), item),
    ):
        redacted = _replace_outside_placeholders(redacted, value)
    redacted = _AUTHORIZATION_BEARER.sub(r"\1" + REDACTED, redacted)
    redacted = _TOKEN_FORM.sub(REDACTED, redacted)
    redacted = _ASSIGNED_SECRET.sub(lambda match: match.group("prefix") + REDACTED, redacted)
    redacted = _AUTHORIZATION_ASSIGNMENT.sub(
        lambda match: match.group("prefix") + REDACTED,
        redacted,
    )
    return redacted


def redact_json(value: Any, sensitive_values: Sequence[str] = ()) -> Any:
    """Recursively redact JSON-like data while preserving non-string scalars."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            rendered_key = str(key)
            if is_sensitive_name(rendered_key):
                result[rendered_key] = _redact_sensitive_descendants(item)
            else:
                result[rendered_key] = redact_json(item, sensitive_values)
        return result
    if isinstance(value, list):
        return [redact_json(item, sensitive_values) for item in value]
    if isinstance(value, tuple):
        return [redact_json(item, sensitive_values) for item in value]
    if isinstance(value, str):
        return redact_text(value, sensitive_values)
    return value


def _replace_outside_placeholders(text: str, sensitive_value: str) -> str:
    """Replace a literal secret without ever rewriting an existing placeholder."""
    return REDACTED.join(
        segment.replace(sensitive_value, REDACTED) for segment in text.split(REDACTED)
    )


def _redact_sensitive_descendants(value: Any) -> Any:
    """Redact every descendant string owned by a sensitive mapping key."""
    if isinstance(value, Mapping):
        return {
            str(key): _redact_sensitive_descendants(item) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive_descendants(item) for item in value]
    if isinstance(value, str):
        return REDACTED
    return value
