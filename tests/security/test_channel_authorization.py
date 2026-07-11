from __future__ import annotations

import sqlite3
from pathlib import Path

from polaris.channels import AuthDecision, AuthorizationPolicy
from polaris.channels.store import ChannelStore


def test_unauthorized_payload_body_is_absent_from_database_and_audit(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    secret = "DO-NOT-PERSIST-UNAUTHORIZED-BODY"
    store = ChannelStore(
        path,
        authorization_policy=AuthorizationPolicy(
            allowed_user_ids=[1],
            allowed_chat_ids=[2],
            allowed_actions=["message", "callback"],
        ),
    )
    denied = store.ingest_telegram_update(
        {
            "update_id": 4,
            "message": {
                "message_id": 1,
                "from": {"id": 999},
                "chat": {"id": 2},
                "text": secret,
            },
        }
    )
    assert denied.decision is AuthDecision.DENY
    audit = store.export_auth_audit()
    assert secret not in repr(audit)
    store.close()

    connection = sqlite3.connect(path)
    row = connection.execute(
        "SELECT payload_json, envelope_json FROM channel_inbox"
    ).fetchone()
    assert row == (None, None)
    all_text = connection.execute(
        """
        SELECT group_concat(
            coalesce(event_type, '') || coalesce(user_id, '') || coalesce(channel_id, '') ||
            coalesce(action, '') || coalesce(reason, '')
        ) FROM channel_auth_audit
        """
    ).fetchone()
    assert secret not in str(all_text)


def test_callback_uses_the_same_deny_by_default_policy(tmp_path: Path) -> None:
    policy = AuthorizationPolicy(
        allowed_user_ids=[1],
        allowed_chat_ids=[2],
        allowed_actions=["message"],
    )
    store = ChannelStore(tmp_path / "journal.sqlite3", authorization_policy=policy)
    result = store.ingest_telegram_update(
        {
            "update_id": 5,
            "callback_query": {
                "id": "cb",
                "from": {"id": 1},
                "data": "sensitive-callback-body",
                "message": {"message_id": 3, "chat": {"id": 2}},
            },
        }
    )
    assert result.decision is AuthDecision.DENY
    assert result.envelope is None
