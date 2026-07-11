"""Immutable records for curated memory."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TrustLevel(StrEnum):
    """How a memory claim was established."""

    USER_ASSERTED = "user_asserted"
    MODEL_INFERRED = "model_inferred"
    VERIFIED = "verified"

    user_asserted = USER_ASSERTED
    model_inferred = MODEL_INFERRED
    verified = VERIFIED


class MemoryKind(StrEnum):
    """The source or purpose of a curated memory."""

    USER = "user"
    AGENT = "agent"
    FACT = "fact"
    PREFERENCE = "preference"

    user = USER
    agent = AGENT
    fact = FACT
    preference = PREFERENCE


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """An explicit isolation boundary for memory operations."""

    profile_id: str
    subject_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not isinstance(self.subject_key, str):
            raise TypeError("profile_id and subject_key must be strings")
        if not self.profile_id.strip() or not self.subject_key.strip():
            raise ValueError("profile_id and subject_key must be non-empty")


@dataclass(frozen=True, slots=True)
class MemoryWrite:
    """Scope-free input accepted by explicit memory write APIs."""

    content: str
    kind: MemoryKind = MemoryKind.FACT
    trust_level: TrustLevel = TrustLevel.USER_ASSERTED
    entry_id: str | None = None
    provenance_run_id: str | None = None
    provenance_session_id: str | None = None
    provenance_message_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.content, str):
            raise TypeError("memory content must be a string")
        if not self.content.strip():
            raise ValueError("memory content must be non-empty")
        if self.entry_id is not None:
            if not isinstance(self.entry_id, str):
                raise TypeError("entry_id must be a string when provided")
            if not self.entry_id.strip():
                raise ValueError("entry_id must be non-empty when provided")
        object.__setattr__(self, "kind", MemoryKind(self.kind))
        object.__setattr__(self, "trust_level", TrustLevel(self.trust_level))


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """One current or historical curated memory value."""

    id: str
    profile_id: str
    subject_key: str
    content: str
    kind: MemoryKind
    trust_level: TrustLevel
    provenance_run_id: str | None
    provenance_session_id: str | None
    provenance_message_id: str | None
    created_at: str
    updated_at: str
    revision: int
    content_hash: str
    blocked_reason: str | None
    tombstoned: bool = False

    @property
    def scope(self) -> MemoryScope:
        return MemoryScope(self.profile_id, self.subject_key)


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """A frozen, deterministic copy of memory intended for one model session."""

    version: int
    hash: str
    entries: tuple[MemoryEntry, ...]


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """A ranked recall result."""

    entry: MemoryEntry
    score: float
    search_backend: str

    @property
    def entry_id(self) -> str:
        return self.entry.id

    @property
    def id(self) -> str:
        return self.entry.id

    @property
    def content(self) -> str:
        return self.entry.content

    @property
    def revision(self) -> int:
        return self.entry.revision

    @property
    def content_hash(self) -> str:
        return self.entry.content_hash

    @property
    def blocked_reason(self) -> str | None:
        return self.entry.blocked_reason
