# Slack channel

Polaris supports one Slack workspace through **Socket Mode only**. It does not
host a public request URL and does not implement workspace OAuth installation or
multi-workspace token storage.

```bash
uv sync --extra channels
# or
uv sync --all-extras
```

## Create a least-privilege app

Create an app at <https://api.slack.com/apps> using this minimal manifest for
public-channel mentions:

```yaml
display_information:
  name: Polaris
features:
  bot_user:
    display_name: Polaris
    always_online: false
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - chat:write
      - channels:history
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.channels
  interactivity:
    is_enabled: true
  socket_mode_enabled: true
```

Then:

1. Under **Basic Information → App-Level Tokens**, generate an app token with
   only `connections:write`; this is the `xapp-…` Socket Mode token.
2. Install the app to the one workspace and copy the `xoxb-…` bot token.
3. Invite the bot only to channels where it should operate.
4. Under **Event Subscriptions**, keep `app_mention` and the message events that
   match the conversation types you actually allow.

For private channels, add only `groups:history` and `message.groups`. For direct
messages, add only `im:history` and `message.im`. Keep `channels:history` and
`message.channels` only for public channels. Polaris does not require
`channels:read`, `groups:read`, user tokens, admin scopes, incoming webhooks, or
`chat:write.public`.

Find user IDs from a member's Slack profile (**More → Copy member ID**) and
channel IDs from **View channel details → About**. IDs are identifiers rather
than credentials, but redact adjacent messages and workspace details in reports.

## Configure and store tokens

Start from [`examples/config.slack.json`](../../examples/config.slack.json) and
replace paths and allowlist placeholders:

```json
{
  "channels": {
    "telegram": {
      "enabled": false
    },
    "slack": {
      "enabled": true,
      "bot_token_env": "SLACK_BOT_TOKEN",
      "app_token_env": "SLACK_APP_TOKEN",
      "allowed_user_ids": ["U0123456789"],
      "allowed_channel_ids": ["C0123456789"],
      "default_provider": "ollama"
    }
  }
}
```

Both allowlists are required and deny by default:

```bash
uv run polaris --config examples/config.slack.json secrets set SLACK_BOT_TOKEN
uv run polaris --config examples/config.slack.json secrets set SLACK_APP_TOKEN
uv run polaris --config examples/config.slack.json secrets check
uv run polarisd --config examples/config.slack.json
```

## Events, commands, and threads

Polaris handles human `app_mention` and `message` events and ignores bot/subtype
messages. The shared command set is:

- `/run <prompt>` (ordinary allowed message text also starts a run)
- `/status RUN_ID`
- `/approve APPROVAL_ID`
- `/deny APPROVAL_ID`
- `/memory add TEXT`, `/memory search QUERY`, `/memory list`
- `/cron list`
- `/help`

These are text commands, not Slack slash-command request URLs. A top-level event
becomes a thread root; replies, final results, and approval-paused notifications
stay in its thread. Memory is scoped to `slack:USER_ID`.

In channels where you subscribe only to `app_mention`, mention the bot before the
command, for example `@Polaris /run summarize the latest deployment`. Polaris
removes the leading app mention before routing the shared text command.

Socket envelopes are acknowledged after durable ingest. Slack `event_id` (with
safe fallbacks), downstream keys, durable inbox/outbox records, and stable
outbound keys suppress local duplicate processing across reconnects/restarts.
As with Telegram, an outbound transport failure can have an unknown remote
outcome. Polaris never blindly retries that state; reconcile it with
`polaris channels unknown`, then `mark-sent` or an explicit audited `retry`.
