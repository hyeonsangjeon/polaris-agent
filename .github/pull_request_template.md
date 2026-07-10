## Summary

<!-- What changed and why? Keep this focused. -->

## Related issue

<!-- Link an issue, or write N/A. -->

## Changes

- N/A

## Verification

<!-- List exact commands and results. Do not write only "tests pass." -->

```text
Command:
Result:
```

## Durability and recovery

<!-- Describe crash windows, leases, retries, reconciliation, replay, and migration impact. Write N/A only when genuinely unrelated. Never claim arbitrary exactly-once. -->

## Security and privacy

<!-- Describe tool authority, approvals, remote bind, secrets, and data exposure. Confirm fixtures/logs are redacted. -->

## Provider and cost impact

<!-- Requested/actual model behavior, API compatibility, token/call budget, and duplicate-billing risk. Write N/A if unrelated. -->

## User-facing documentation

<!-- List updated docs/examples, or explain why none are needed. -->

## UI evidence

<!-- Add accessible screenshots for UI changes. Do not include tokens, prompts, private paths, or user data. Write N/A otherwise. -->

## Checklist

- [ ] I kept the change focused and preserved existing attribution.
- [ ] I ran the relevant Python, frontend, Rust, container, or docs checks.
- [ ] I added or updated tests for behavior changes.
- [ ] I documented recovery semantics without an exactly-once overclaim.
- [ ] I removed secrets and sensitive run data from commits and screenshots.
- [ ] I completed every section above, using N/A with a reason where needed.
