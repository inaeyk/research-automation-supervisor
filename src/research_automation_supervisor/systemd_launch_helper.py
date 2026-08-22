#!/usr/bin/env python3
"""Secret-free stdin-framed environment loader for a transient service."""

from __future__ import annotations

import json
import os
import struct
import sys

_MAGIC = b"RASENV1\n"
_MAX_ENVIRONMENT_BYTES = 16 * 1024 * 1024


def encode_environment_frame(environment: dict[str, str]) -> bytes:
    """Encode an exact sanitized environment without placing values in argv."""
    payload = json.dumps(
        environment,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(payload) > _MAX_ENVIRONMENT_BYTES:
        raise ValueError("sanitized environment frame is too large")
    return _MAGIC + struct.pack(">Q", len(payload)) + payload


def _read_exact(count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        chunk = os.read(sys.stdin.fileno(), count - len(chunks))
        if not chunk:
            raise ValueError("sanitized environment frame ended early")
        chunks.extend(chunk)
    return bytes(chunks)


def main() -> int:
    if len(sys.argv) < 2:
        return 125
    try:
        if _read_exact(len(_MAGIC)) != _MAGIC:
            return 125
        size = struct.unpack(">Q", _read_exact(8))[0]
        if size > _MAX_ENVIRONMENT_BYTES:
            return 125
        decoded = json.loads(_read_exact(size))
        if not isinstance(decoded, dict) or not all(
            isinstance(name, str)
            and isinstance(value, str)
            and name
            and "=" not in name
            and "\x00" not in name
            and "\x00" not in value
            for name, value in decoded.items()
        ):
            return 125
    except (OSError, ValueError, json.JSONDecodeError, struct.error):
        return 125
    os.execve(sys.argv[1], sys.argv[1:], decoded)
    return 125


if __name__ == "__main__":
    raise SystemExit(main())
