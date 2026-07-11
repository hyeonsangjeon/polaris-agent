"""Threat scanning and redaction for data crossing the memory boundary."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

BLOCKED_CONTENT = "[BLOCKED MEMORY: unsafe content omitted]"

_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "prompt_injection:ignore_previous",
        re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?\b", re.I),
    ),
    ("prompt_injection:system_prompt", re.compile(r"\bsystem\s+prompt\b", re.I)),
    (
        "prompt_injection:tool_execution",
        re.compile(
            r"\b(?:execute|invoke|call|run|use)\s+(?:the\s+)?"
            r"(?:tool|command|shell|terminal)\b",
            re.I,
        ),
    ),
    (
        "prompt_injection:role_takeover",
        re.compile(
            r"\b(?:you\s+are\s+now|act\s+as|switch\s+(?:your\s+)?role\s+to|"
            r"new\s+(?:system|developer)\s+instructions?)\b",
            re.I,
        ),
    ),
    ("prompt_injection:fence_breakout", re.compile(r"```")),
    (
        "prompt_injection:memory_tag_breakout",
        re.compile(
            r"</?\s*(?:memory|memory_context|polaris-memory|system|developer)(?:\s|>|/)",
            re.I,
        ),
    ),
)

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "secret:private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
            r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
            re.I | re.S,
        ),
    ),
    (
        "secret:jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    ),
    (
        "secret:api_token",
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|"
            r"github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ThreatScan:
    """The stable, non-sensitive result of scanning one memory value."""

    blocked_reason: str | None

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None


class ThreatScanner:
    """Detect known prompt-injection forms and secrets without external dependencies."""

    def __init__(self, configured_secrets: Iterable[str] = ()) -> None:
        if isinstance(configured_secrets, str):
            configured_secrets = (configured_secrets,)
        self._configured_secrets = tuple(
            sorted(
                {secret for secret in configured_secrets if secret},
                key=lambda item: (-len(item), item),
            )
        )

    def scan(self, content: str) -> ThreatScan:
        reasons = [name for name, pattern in _INJECTION_PATTERNS if pattern.search(content)]
        reasons.extend(name for name, pattern in _SECRET_PATTERNS if pattern.search(content))
        if any(secret in content for secret in self._configured_secrets):
            reasons.append("secret:configured")
        unique = tuple(dict.fromkeys(reasons))
        return ThreatScan(";".join(unique) if unique else None)

    def redact(self, content: str) -> str:
        redacted = content
        for _name, pattern in _SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED SECRET]", redacted)
        for secret in self._configured_secrets:
            redacted = redacted.replace(secret, "[REDACTED CONFIGURED SECRET]")
        return redacted
