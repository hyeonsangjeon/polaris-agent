"""Deny-by-default channel authorization."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .models import AuthDecision, Platform


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    decision: AuthDecision
    reason: str


class AuthorizationPolicy:
    """Authorize channel events using platform-scoped identity, channel, and action sets."""

    def __init__(
        self,
        *,
        allowed_user_ids: Iterable[str | int] = (),
        allowed_channel_ids: Iterable[str | int] = (),
        allowed_chat_ids: Iterable[str | int] = (),
        allowed_actions: Iterable[str] = (),
        platform_users: dict[Platform | str, Iterable[str | int]] | None = None,
        platform_channels: dict[Platform | str, Iterable[str | int]] | None = None,
    ) -> None:
        self._users = frozenset(str(value) for value in allowed_user_ids)
        channels = (*allowed_channel_ids, *allowed_chat_ids)
        self._channels = frozenset(str(value) for value in channels)
        self._actions = frozenset(str(value) for value in allowed_actions)
        self._platform_users = {
            Platform(platform): frozenset(str(value) for value in values)
            for platform, values in (platform_users or {}).items()
        }
        self._platform_channels = {
            Platform(platform): frozenset(str(value) for value in values)
            for platform, values in (platform_channels or {}).items()
        }

    def evaluate(
        self,
        platform: Platform | str,
        user_id: str | int,
        channel_id: str | int,
        action: str,
    ) -> AuthorizationResult:
        selected_platform = Platform(platform)
        user = str(user_id)
        channel = str(channel_id)
        users = self._platform_users.get(selected_platform, self._users)
        channels = self._platform_channels.get(selected_platform, self._channels)
        if not users:
            return AuthorizationResult(AuthDecision.DENY, "no users are authorized")
        if user not in users:
            return AuthorizationResult(AuthDecision.DENY, "user is not authorized")
        if not channels:
            return AuthorizationResult(AuthDecision.DENY, "no channels are authorized")
        if channel not in channels:
            return AuthorizationResult(AuthDecision.DENY, "channel is not authorized")
        if not self._actions:
            return AuthorizationResult(AuthDecision.DENY, "no actions are authorized")
        if action not in self._actions:
            return AuthorizationResult(AuthDecision.DENY, "action is not authorized")
        return AuthorizationResult(AuthDecision.ALLOW, "authorized")

    def authorize(
        self,
        platform: Platform | str,
        user_id: str | int,
        channel_id: str | int,
        action: str,
    ) -> AuthDecision:
        return self.evaluate(platform, user_id, channel_id, action).decision
