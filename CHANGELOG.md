# Changelog

All notable changes to Polaris Agent are documented here. This project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses semantic
versioning.

## [Unreleased]

## [0.2.0] - 2026-07-11

### Added

- Explicitly curated SQLite memory with profile/subject isolation, trust and
  provenance, threat-scanned FTS/LIKE recall, frozen per-run snapshots, CLI/API,
  and scope-bound model tools.
- Durable once, interval, and five-field cron jobs with IANA timezone/DST
  handling, catch-up policy, transactional occurrence dedupe, CLI/API, and
  optional channel delivery.
- Telegram text commands through long polling and single-workspace Slack Socket
  Mode, with deny-by-default user/conversation allowlists and durable
  inbox/outbox/event dedupe.
- Shared remote commands for runs, status, approvals, curated memory, schedule
  listing, and help; final and approval-paused run notifications.
- Owner-only `runtime-secrets.env` with strict non-shell parsing and
  `polaris secrets set/list/remove/check`.
- Memory, Schedules, and Channels views in the independent desktop client.

### Changed

- Backups explicitly exclude the API token and runtime secrets file.
- Launchd service configuration includes only the runtime secrets file path.
- Optional Telegram/Slack dependencies are available through the `channels`
  extra; `--all-extras` installs every optional integration.

### Migration from v0.1

- Existing v0.1 journals and Ollama/Foundry configurations remain compatible.
  Memory, scheduler, and channel tables are created automatically at daemon
  startup.
- Memory and scheduler default to enabled. Telegram and Slack default to
  disabled and require explicit token-variable names plus non-empty user and
  channel/chat allowlists.
- Existing runs, approvals, replay, artifacts, provider selection, and tool
  durability semantics are unchanged.

### Security

- Remote channel input is denied unless both sender and destination are
  allowlisted. Unknown outbound outcomes are persisted and never blindly
  retried.
- Scheduled Telegram and Slack delivery targets are revalidated against the
  configured channel allowlist before entering the outbox.
- Memory writes remain explicit; stored content is untrusted and must never be
  used for secrets.

## [0.1.0] - 2026-07-11

### Added

- Durable SQLite journal with leases, receipts, approvals, budgets, events, and
  content-addressed artifacts.
- Single-agent, bounded Ollama fan-out, and thin Foundry Model Router modes.
- Crash recovery for eligible expired work and operator stops for uncertain
  opaque effects.
- Deterministic replay of committed records and evidence-oriented ensemble
  outputs.
- Bearer-authenticated loopback daemon, CLI, launchd integration, independent
  Tauri macOS console, and Docker Compose deployment surfaces.
- Ollama and Foundry provider contracts with API-key and Entra authentication.

[Unreleased]: https://github.com/hyeonsangjeon/polaris-agent/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/hyeonsangjeon/polaris-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/hyeonsangjeon/polaris-agent/releases/tag/v0.1.0
