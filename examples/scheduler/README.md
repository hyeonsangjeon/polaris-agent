# Scheduler examples

Create the JSON request through the API:

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $POLARIS_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data @examples/scheduled-job.request.json \
  http://127.0.0.1:8765/v1/jobs
```

Equivalent CLI:

```bash
uv run polaris cron add weekday-summary "0 9 * * 1-5" \
  "Summarize open work and include repository evidence." \
  --timezone Asia/Seoul --provider ollama \
  --catchup bounded --max-catchup 3
```

To deliver a completed scheduled run, add
`--delivery-platform telegram --delivery-channel CHAT_ID` or
`--delivery-platform slack --delivery-channel CHANNEL_ID`. The relevant channel
must be enabled. Use an ID from its configured allowlist; v0.2 does not
revalidate an explicit scheduled outbound target against the inbound allowlist.
