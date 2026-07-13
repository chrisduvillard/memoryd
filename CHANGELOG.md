# Changelog

## 0.3.0 — 2026-07-13

memoryd 0.3.0 and its Hermes plugin ship as one release.

### Added

- Crash-durable, profile-scoped Hermes mutation queues with persisted retries,
  stale-claim recovery, permanent-failure evidence, and queue health reporting.
- Idempotent `request_id` handling for capture, extraction, and miss reports,
  backed by migration `007_api_request_ledger.sql`.
- Offline `backup create|list|verify|restore` commands, sanitized manifests,
  checksums, migration inventories, and safe out-of-place restore.
- A Linux user timer that creates and verifies a daily snapshot at 02:35,
  restarts memoryd after success or failure, and retains 14 valid snapshots.
- Exact Hermes Agent 0.16.0 compatibility checks against source tag
  `v2026.6.5`, commit
  `3c231eb3979ab9c57d5cd6d02f1d577a3b718b43`.

### Changed

- Fresh Docker installs generate a random PostgreSQL password, bind PostgreSQL
  to localhost, and keep the DSN in owner-readable configuration.
- The Hermes plugin queues capture, extraction, and miss mutations on disk
  before returning from a primary-context hook. In-memory fallback buffering
  has been removed.
- Release gates now test the pinned Hermes contract on pull requests and run a
  nonblocking compatibility check against upstream `main` each week.

### Operations

- Follow [the production rollout](docs/PRODUCTION_ROLLOUT.md) for the clean
  Linux installation and disposable restore drill.
- Do not promote the installation before it passes the complete
  [14-day/200-turn scorecard](docs/CANARY_SCORECARD.md).
- This release does not read, repair, delete, or migrate the existing Windows
  installation or its data.
