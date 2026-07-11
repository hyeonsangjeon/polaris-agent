"""Telegram-safe text formatting and UTF-16 chunking."""

from __future__ import annotations

import html

from .models import OutboundMessage

TELEGRAM_TEXT_LIMIT = 4096


def utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def safe_html(value: str) -> str:
    return html.escape(value, quote=False)


def _take_units(value: str, limit: int) -> tuple[str, str]:
    used = 0
    last_break = 0
    end = 0
    for index, character in enumerate(value):
        units = 2 if ord(character) > 0xFFFF else 1
        if used + units > limit:
            break
        used += units
        end = index + 1
        if character in "\n \t":
            last_break = end
    if end == len(value):
        return value, ""
    if last_break and last_break >= end // 2:
        end = last_break
    return value[:end], value[end:]


def chunk_text(value: str, *, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split on readable boundaries without ever exceeding Telegram's UTF-16 limit."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if not value:
        return [""]
    chunks: list[str] = []
    remaining = value
    while remaining:
        chunk, remaining = _take_units(remaining, limit)
        if not chunk:
            raise ValueError("limit is too small for a Unicode code point")
        chunks.append(chunk)
    return _preserve_code_fences(chunks, limit)


def _preserve_code_fences(chunks: list[str], limit: int) -> list[str]:
    if len(chunks) < 2 or "```" not in "".join(chunks):
        return chunks
    result: list[str] = []
    in_fence = False
    for index, original in enumerate(chunks):
        chunk = original
        prefix = "```\n" if in_fence else ""
        if prefix and utf16_units(prefix + chunk) <= limit:
            chunk = prefix + chunk
        elif prefix:
            return chunks
        in_fence ^= original.count("```") % 2 == 1
        if in_fence and index < len(chunks) - 1:
            suffix = "\n```"
            if utf16_units(chunk + suffix) <= limit:
                chunk += suffix
            else:
                return chunks
        result.append(chunk)
    return result


def chunk_outbound(
    message: OutboundMessage, *, limit: int = TELEGRAM_TEXT_LIMIT
) -> list[OutboundMessage]:
    chunks = chunk_text(message.text, limit=limit)
    count = len(chunks)
    if count == 1:
        return [message]
    return [
        OutboundMessage(
            platform=message.platform,
            idempotency_key=f"{message.idempotency_key}:chunk:{index}",
            channel_id=message.channel_id,
            thread_key=message.thread_key,
            text=text,
            operation=message.operation,
            parse_mode=message.parse_mode,
            message_id=message.message_id,
            callback_query_id=message.callback_query_id,
            disable_notification=message.disable_notification,
            chunk_index=index,
            chunk_count=count,
            metadata=message.metadata,
        )
        for index, text in enumerate(chunks)
    ]
