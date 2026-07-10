"""Canonical serialization helpers used by the journal."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Any


def utc_now() -> str:
    """Return the current UTC time in a lexically sortable ISO representation."""
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def normalize_timestamp(value: str | datetime | None) -> str:
    """Normalize a timestamp to UTC ISO-8601."""
    if value is None:
        return utc_now()
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return normalize_timestamp(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def canonical_json(value: object) -> str:
    """Serialize JSON deterministically for hashing and durable storage."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def sha256_hex(value: object) -> str:
    """Return a SHA-256 digest of canonical JSON, or of bytes as supplied."""
    data = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def deep_freeze(value: Any) -> Any:
    """Recursively freeze decoded JSON values."""
    if isinstance(value, dict):
        return MappingProxyType({key: deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(deep_freeze(item) for item in value)
    return value


def decode_json(value: str | None) -> Any:
    """Decode stored JSON into immutable containers."""
    if value is None:
        return None
    return deep_freeze(json.loads(value))
