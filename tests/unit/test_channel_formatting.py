from __future__ import annotations

from polaris.channels import OutboundMessage, Platform, chunk_outbound, chunk_text, utf16_units


def test_utf16_chunk_bounds_include_astral_characters() -> None:
    text = ("a" * 4095) + "😀" + ("b" * 20)
    chunks = chunk_text(text)
    assert "".join(chunks) == text
    assert len(chunks) > 1
    assert all(utf16_units(chunk) <= 4096 for chunk in chunks)


def test_outbound_chunk_keys_and_indexes_are_deterministic() -> None:
    message = OutboundMessage(
        platform=Platform.TELEGRAM,
        idempotency_key="run:reply",
        channel_id="20",
        thread_key="telegram:20",
        text="x" * 5000,
    )
    chunks = chunk_outbound(message)
    assert [item.idempotency_key for item in chunks] == [
        "run:reply:chunk:0",
        "run:reply:chunk:1",
    ]
    assert [item.chunk_index for item in chunks] == [0, 1]
    assert all(item.chunk_count == 2 for item in chunks)
