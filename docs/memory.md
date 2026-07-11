# Curated memory

Polaris memory is an explicit, local SQLite collection—not a transcript scraper
or an instruction channel. Entries are isolated by both `profile_id` and
`subject_key`; a caller must name the scope on every read or write.

## Data and trust

Each entry records:

- content, kind (`user`, `agent`, `fact`, or `preference`), and trust level
  (`user_asserted`, `model_inferred`, or `verified`);
- revision and content hash;
- optional run, session, and message provenance; and
- tombstone and threat-scan state.

Trust is descriptive provenance, not authorization. `verified` should mean that
the operator verified the claim; it does not make the content an instruction.
Polaris never turns model output or conversation text into memory
automatically. All writes use the CLI, API, channel command, or an explicit
approval-gated model tool.

Before a single-agent run starts, Polaris freezes a bounded snapshot of its
scope. The snapshot version and hash are persisted with the run, so edits made
during execution do not alter that run's prompt context. Memory is rendered as
untrusted data. Known prompt-injection forms, secret patterns, configured secret
values, and content-hash mismatches are blocked from prompt recall. This scanner
is defense in depth, not a complete content-security system.

Recall uses SQLite FTS5 when available and a deterministic scoped `LIKE`
fallback otherwise. Search never crosses a profile/subject boundary.

## Configuration

```json
{
  "memory": {
    "enabled": true,
    "profile_id": "default",
    "char_budget": 12000,
    "tool_enabled": true
  }
}
```

Disabling `memory.enabled` stops snapshot injection. `tool_enabled` controls the
scope-bound model tools: `memory_search`, `memory_add`, `memory_revise`, and
`memory_remove`. Search is read-only; writes are reconcilable effects and remain
subject to normal approval policy.

## CLI

The default scope is profile `default`, subject `local`.

```bash
uv run polaris memory add \
  "Prefer concise release summaries." \
  --profile default --subject local \
  --kind preference --trust user_asserted

uv run polaris memory list --profile default --subject local --json
uv run polaris memory search "release" --profile default --subject local

uv run polaris memory revise ENTRY_ID \
  "Prefer concise release notes with migration steps." \
  --revision 1 --profile default --subject local

uv run polaris memory remove ENTRY_ID \
  --revision 2 --profile default --subject local
```

Use `--hash CONTENT_HASH` with revise/remove for an additional optimistic
concurrency check. The CLI supports run provenance on add with `--run-id`.

## API

All requests require the daemon bearer token.

```bash
curl -fsS -X POST \
  -H "Authorization: Bearer $POLARIS_API_TOKEN" \
  -H "Content-Type: application/json" \
  http://127.0.0.1:8765/v1/memory \
  -d '{
    "profile_id": "default",
    "subject_key": "local",
    "content": "Prefer concise release summaries.",
    "kind": "preference",
    "trust_level": "user_asserted",
    "provenance_run_id": "run_REPLACE_ME"
  }'

curl -fsS -G \
  -H "Authorization: Bearer $POLARIS_API_TOKEN" \
  --data-urlencode "profile_id=default" \
  --data-urlencode "subject_key=local" \
  --data-urlencode "query=release" \
  http://127.0.0.1:8765/v1/memory/search
```

`PUT /v1/memory/{entry_id}` revises an entry and
`DELETE /v1/memory/{entry_id}` tombstones it; both require
`expected_revision`.

## Do not store secrets

Memory is persisted in the journal, included in state backups, and may be shown
to a model. Never store tokens, passwords, private keys, recovery codes, or
confidential file contents. The scanner blocks several known forms but cannot
identify every secret. Use [runtime secrets](secrets.md) instead.
