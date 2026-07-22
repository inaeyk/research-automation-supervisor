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
    redacted = _redact_sensitive_literals(text, sensitive_values)
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


def _redact_sensitive_literals(text: str, sensitive_values: Sequence[str]) -> str:
    """Replace overlapping literals while preserving standalone placeholders."""
    literals = sorted(
        {value for value in sensitive_values if value and value != REDACTED},
        key=lambda item: (-len(item), item),
    )
    if not literals:
        return text

    placeholder_spans = [match.span() for match in re.finditer(re.escape(REDACTED), text)]
    matcher = re.compile(f"(?=({'|'.join(re.escape(value) for value in literals)}))")
    sensitive_spans: list[tuple[int, int]] = []
    for match in matcher.finditer(text):
        start, end = match.span(1)
        containing_placeholder = next(
            (
                (placeholder_start, placeholder_end)
                for placeholder_start, placeholder_end in placeholder_spans
                if placeholder_start <= start and end <= placeholder_end
            ),
            None,
        )
        if containing_placeholder is not None:
            continue
        for placeholder_start, placeholder_end in placeholder_spans:
            if start < placeholder_end and placeholder_start < end:
                start = min(start, placeholder_start)
                end = max(end, placeholder_end)
        sensitive_spans.append((start, end))

    if not sensitive_spans:
        return text
    merged_spans: list[tuple[int, int]] = []
    for start, end in sorted(sensitive_spans):
        if merged_spans and start <= merged_spans[-1][1]:
            previous_start, previous_end = merged_spans[-1]
            merged_spans[-1] = (previous_start, max(previous_end, end))
        else:
            merged_spans.append((start, end))

    pieces: list[str] = []
    offset = 0
    for start, end in merged_spans:
        pieces.extend((text[offset:start], REDACTED))
        offset = end
    pieces.append(text[offset:])
    return "".join(pieces)


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
