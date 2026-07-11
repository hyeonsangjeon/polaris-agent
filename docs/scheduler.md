# Durable scheduler

The optional scheduler creates single-agent, fan-out, or Foundry Router runs
from durable SQLite jobs. It supports one-time timestamps, fixed-second
intervals, and numeric five-field cron expressions:

```text
minute hour day-of-month month day-of-week
```

Fields support numbers, `*`, lists, ranges, and steps. Named months/weekdays,
seconds, and cron macros are not accepted.

The scheduler uses one transactional claim path and a unique
`(job_id, scheduled_for)` identity to deduplicate local dispatch. This is not an
exactly-once guarantee for model billing, tools, or remote delivery.

## CLI

```bash
# One local time, interpreted in the named IANA timezone
uv run polaris cron once daily-note "2026-07-12T09:00:00" \
  "Summarize open work." --timezone Asia/Seoul --provider ollama

# Every 30 minutes
uv run polaris cron interval health-note 1800 \
  "Report repository health." --catchup fire_once

# Weekdays at 09:00
uv run polaris cron add weekday-summary "0 9 * * 1-5" \
  "Summarize open work." --timezone Asia/Seoul \
  --catchup bounded --max-catchup 3

uv run polaris cron preview "0 9 * * 1-5" \
  "2026-07-11T00:00:00Z" --timezone Asia/Seoul --count 5
uv run polaris cron list
uv run polaris cron show JOB_ID
uv run polaris cron runs --job JOB_ID
uv run polaris cron pause JOB_ID
uv run polaris cron resume JOB_ID
uv run polaris cron cancel JOB_ID
```

`--delivery-platform telegram|slack --delivery-channel ID` requests optional
delivery of the resulting run to an enabled, configured channel.

## API request

[`examples/scheduled-job.request.json`](../examples/scheduled-job.request.json)
is a complete `POST /v1/jobs` body:

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $POLARIS_API_TOKEN" \
  -H "Content-Type: application/json" \
  --data @examples/scheduled-job.request.json \
  http://127.0.0.1:8765/v1/jobs
```

Important routes:

| Method and path | Purpose |
|---|---|
| `POST /v1/jobs/preview` | Preview a schedule |
| `POST /v1/jobs` | Create a job |
| `GET /v1/jobs` | List jobs |
| `GET /v1/jobs/{job_id}` | Inspect one job |
| `POST /v1/jobs/{job_id}/pause` | Pause future dispatch |
| `POST /v1/jobs/{job_id}/resume` | Resume future dispatch |
| `POST /v1/jobs/{job_id}/cancel` | Cancel future dispatch |
| `GET /v1/jobs/runs?job_id=...` | List scheduled occurrences |
| `POST /v1/jobs/runs/{run_id}/retry` | Explicitly retry an unsuccessful occurrence |

## Timezone and DST

Use IANA names such as `UTC`, `Asia/Seoul`, or `America/New_York`. Cron is
evaluated against local wall-clock fields but stored as UTC instants.
Nonexistent local one-time timestamps during a spring-forward gap are rejected.
An ambiguous one-time timestamp during a fall-back fold selects its first
occurrence. Cron iteration evaluates actual UTC minutes, so a matching repeated
wall-clock minute can occur twice; a nonexistent wall-clock minute does not
occur. Preview schedules around DST before enabling them.

## Catch-up

When the daemon was not running:

- `skip` advances beyond missed occurrences without firing them;
- `fire_once` runs only the latest missed occurrence; and
- `bounded` runs up to `max_catchup` most recent occurrences.

`max_catchup` is valid only for `bounded` and is limited to 10. The scheduler
also caps startup claims according to `scheduler.startup_cap`.

## Interruption and retry

A stale claimed/running occurrence becomes `interrupted` after its lease
expires, with an ambiguity warning. It is **not** retried automatically. Inspect
the related target and Polaris run, then explicitly retry:

```bash
uv run polaris cron runs --status interrupted --json
uv run polaris cron retry JOB_RUN_ID
```

Retry reuses the scheduled occurrence record, increments its attempt, and may
repeat provider charges or external effects.

## Delivery

Execution and delivery have separate status. Polaris marks the scheduled
occurrence successful after it creates the Polaris run; channel delivery can
then succeed, fail, or be suppressed independently. Delivery uses the durable
channel outbox. A network failure with an unknown remote outcome is never
blindly retried—inspect `polaris channels unknown`, then explicitly
`mark-sent` or `retry` with an audit note. See [channel durability](durability.md#channel-events-and-delivery).

Use a delivery channel/chat ID from the configured allowlist. In v0.2 the
scheduler's explicit outbound target is not revalidated against the inbound
allowlist; treat this as an operator-controlled configuration boundary.
