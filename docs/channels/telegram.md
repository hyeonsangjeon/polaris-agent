# Telegram channel

Polaris supports one Telegram bot through **long polling only**. It has no
webhook/public-ingress or media support. Install the optional dependencies first:

```bash
uv sync --extra channels
# or install every optional integration:
uv sync --all-extras
```

## Create the bot and identify the allowlist

1. In Telegram, open the verified `@BotFather` account.
2. Send `/newbot`, follow the prompts, and copy the bot token into a private
   password manager. Never paste it into JSON, an issue, or shell history.
3. Message the new bot from each intended user/chat.
4. Before starting Polaris polling, inspect `getUpdates` locally. Prompt for the
   token without echoing it:

   ```bash
   read -s TELEGRAM_BOT_TOKEN
   export TELEGRAM_BOT_TOKEN
   uv run python - <<'PY'
   import json
   import os
   import httpx

   token = os.environ["TELEGRAM_BOT_TOKEN"]
   response = httpx.post(
       f"https://api.telegram.org/bot{token}/getUpdates",
       json={"timeout": 0, "allowed_updates": ["message"]},
       timeout=10,
   )
   response.raise_for_status()
   print(json.dumps(response.json().get("result", []), indent=2))
   PY
   unset TELEGRAM_BOT_TOKEN
   ```

   Copy numeric `message.from.id` values into `allowed_user_ids` and
   `message.chat.id` values into `allowed_chat_ids`. Do not use an untrusted
   third-party ID bot. Redact message text before sharing this output.

Only one long poller may use a bot token. Polaris calls `deleteWebhook` without
dropping pending updates at startup and stops after repeated poller conflicts.

## Configure and store the token

Start from [`examples/config.telegram.json`](../../examples/config.telegram.json).
Replace absolute paths and numeric placeholders:

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token_env": "TELEGRAM_BOT_TOKEN",
      "allowed_user_ids": [123456789],
      "allowed_chat_ids": [123456789],
      "default_provider": "ollama"
    },
    "slack": {
      "enabled": false
    }
  }
}
```

Both allowlists are required and deny by default. A message is accepted only
when its user **and** chat are listed. Store the value in the owner-only runtime
secrets file:

```bash
uv run polaris --config examples/config.telegram.json \
  secrets set TELEGRAM_BOT_TOKEN
uv run polaris --config examples/config.telegram.json secrets check
uv run polarisd --config examples/config.telegram.json
```

## Commands

- `/run <prompt>` (plain text also starts a run)
- `/status RUN_ID`
- `/approve APPROVAL_ID`
- `/deny APPROVAL_ID`
- `/memory add TEXT`, `/memory search QUERY`, `/memory list`
- `/cron list` (remote scheduling is read-only in v0.2)
- `/help`

Memory is scoped to `telegram:USER_ID`. Final results and approval-paused
notifications return to the originating chat. Text is safely escaped/chunked;
forum topics, inline callbacks without a chat message, non-text messages, files,
images, audio, and video are ignored.

## Restarts and duplicates

The SQLite channel store commits each Telegram update and the next offset in one
transaction. Update IDs and downstream keys deduplicate redelivery. Inbox and
outbox leases are recovered after restart, and outbound chunks have stable
idempotency keys. Telegram itself has no general idempotency key for sends: if a
transport fails after Telegram accepted a message, Polaris marks delivery
`unknown` and does not retry blindly. Resolve it with `polaris channels unknown`,
`mark-sent`, or an explicit audited `retry`.
