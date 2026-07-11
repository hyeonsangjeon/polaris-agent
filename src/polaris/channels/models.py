"""Domain models shared by durable channel adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class Platform(StrEnum):
    TELEGRAM = "telegram"
    SLACK = "slack"

    telegram = TELEGRAM
    slack = SLACK


class InboxStatus(StrEnum):
    RECEIVED = "received"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

    received = RECEIVED
    processing = PROCESSING
    completed = COMPLETED
    failed = FAILED


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    UNKNOWN = "unknown"
    FAILED = "failed"

    pending = PENDING
    sending = SENDING
    sent = SENT
    unknown = UNKNOWN
    failed = FAILED


class AuthDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    IGNORED = "ignored"

    allow = ALLOW
    deny = DENY
    ignored = IGNORED


class MessageOperation(StrEnum):
    SEND = "send_message"
    EDIT = "edit_message"
    ANSWER_CALLBACK = "answer_callback"


class ParseMode(StrEnum):
    PLAIN = "plain"
    HTML = "html"


@dataclass(frozen=True, slots=True)
class ChannelEnvelope:
    platform: Platform
    external_event_id: str
    event_type: str
    user_id: str
    channel_id: str
    thread_key: str
    downstream_key: str
    text: str | None = None
    message_id: str | None = None
    callback_query_id: str | None = None
    callback_data: str | None = None
    action: str = "message"
    received_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return self.event_type


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    platform: Platform
    idempotency_key: str
    channel_id: str
    thread_key: str
    text: str
    operation: MessageOperation = MessageOperation.SEND
    parse_mode: ParseMode = ParseMode.PLAIN
    message_id: str | None = None
    callback_query_id: str | None = None
    disable_notification: bool = False
    chunk_index: int = 0
    chunk_count: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RemoteReceipt:
    platform: Platform
    idempotency_key: str
    remote_message_id: str | None
    channel_id: str
    operation: MessageOperation
    remote_message_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InboxRecord:
    envelope: ChannelEnvelope
    status: InboxStatus
    auth_decision: AuthDecision
    auth_reason: str
    lease_owner: str | None
    lease_expires_at: str | None
    attempt_count: int
    run_id: str | None
    outbox_key: str | None
    error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    message: OutboundMessage
    status: OutboxStatus
    content_hash: str
    lease_owner: str | None
    lease_expires_at: str | None
    attempt_count: int
    remote_receipt: RemoteReceipt | None
    error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class IngestResult:
    envelope: ChannelEnvelope | None
    decision: AuthDecision
    reason: str
    duplicate: bool
    next_offset: int | None

    @property
    def accepted(self) -> bool:
        return self.decision is AuthDecision.ALLOW and not self.duplicate


@runtime_checkable
class ChannelAdapter(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def send(self, message: OutboundMessage) -> RemoteReceipt: ...
