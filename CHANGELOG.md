# Changelog

All notable changes to Polaris Agent are documented here. This project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses semantic
versioning.

## [Unreleased]

### Changed

- Coverage enforcement is being raised separately from the functional test
  matrix.

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

[Unreleased]: https://github.com/hyeonsangjeon/polaris-agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/hyeonsangjeon/polaris-agent/releases/tag/v0.1.0
