"""Complete Stage 3 confidentiality preflight using the Stage 1 redactor."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import NoReturn

from pydantic import BaseModel

from research_automation_supervisor.errors import (
    ShadowConfidentialityError,
    ShadowInputError,
    ShadowIntegrityError,
)
from research_automation_supervisor.redaction import would_redact_text


def iter_shadow_strings(value: object) -> tuple[str, ...]:
    """Return every recursively persisted or transmitted string, including keys."""
    found: list[str] = []

    def visit(item: object) -> None:
        if isinstance(item, str):
            found.append(item)
            return
        if isinstance(item, bytes):
            try:
                found.append(item.decode("utf-8"))
            except UnicodeDecodeError:
                return
            return
        if isinstance(item, BaseModel):
            visit(item.model_dump(mode="json"))
            return
        if isinstance(item, Mapping):
            for key, nested in item.items():
                visit(str(key))
                visit(nested)
            return
        if isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(found)


def preflight_shadow_confidentiality(
    value: object,
    sensitive_values: Sequence[str],
    *,
    label: str,
    integrity: bool = False,
) -> None:
    """Reject any string the single Stage 1 redaction policy would modify."""
    for text in iter_shadow_strings(value):
        if would_redact_text(text, sensitive_values):
            _raise_confidentiality(label, integrity)


def preflight_shadow_locator(
    value: str | os.PathLike[str],
    sensitive_values: Sequence[str],
    *,
    label: str,
) -> str:
    """Check the exact caller-supplied lexical locator before path handling."""
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise ShadowInputError(f"{label} is not a valid path locator") from exc
    if not isinstance(raw, str):
        raise ShadowInputError(f"{label} is not a text path locator")
    preflight_shadow_confidentiality(
        raw,
        sensitive_values,
        label=label,
    )
    return raw


def _raise_confidentiality(label: str, integrity: bool) -> NoReturn:
    message = f"{label} contains a confidentiality-policy collision"
    if integrity:
        raise ShadowIntegrityError(message)
    raise ShadowConfidentialityError(message)
