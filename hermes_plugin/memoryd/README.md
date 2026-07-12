# memoryd — Hermes memory provider

Connects Hermes to a running [memoryd](../../..) daemon: deterministic
per-turn recall (prefetch), full turn capture into the canonical event
ledger, mirroring of built-in MEMORY.md writes, pre-compression snapshots,
subagent delegation records, and end-of-session extraction. Memory is
shared with every other agent on the substrate (Claude Code, etc.) under
per-agent visas.

## Install

1. Run the memoryd daemon (see the main memoryd README).
2. Copy this directory to `<hermes install>/plugins/memory/memoryd/`
   (or `~/.hermes/plugins/memory/memoryd/` for user plugins).
3. Activate:
   ```
   hermes memory setup        # pick "memoryd", set url if non-default
   # or: hermes config set memory.provider memoryd
   ```
4. Verify: `hermes memoryd status`

## Behavior & guarantees

- **Crash-durable capture.** Every primary-context capture, extraction, and
  miss report is fsynced before its hook returns, then drained in order by a
  background worker. Jobs live under the active profile's
  `$HERMES_HOME/spool/memoryd/{incoming,processing,dead-letter}`. A daemon
  outage or Hermes restart leaves jobs available for retry; stale claims are
  recovered after 15 minutes.
- **Fail-open, never silent.** Daemon outages do not block Hermes. Network
  failures, HTTP 408/429, and 5xx responses retry with persisted exponential
  backoff (capped at 5 minutes). Other 4xx responses retain the full job and
  reason in `dead-letter`. A disk-persistence failure warns on stderr and
  injects one visible capture-durability marker; it is never reported as a
  successful queue operation.
- **Idempotent delivery.** Each job carries a stable request id and body
  digest. A lost success response is retried safely against memoryd's durable
  request-id handling. Jobs are deleted only after a 2xx JSON response.
- **Non-primary contexts never write.** Cron/subagent/flush contexts are
  read-only toward memory and do not create queue state — a cron prompt can't
  corrupt user memory.
  Subagent work still reaches the ledger via the parent's `on_delegation`.
- **Built-in MEMORY.md stays active** (Hermes always keeps it). This
  provider treats it as a vendor cache: writes are mirrored to canonical
  via `on_memory_write`, so nothing lives only in the vendor layer.
- **Visa scoping.** The daemon serves this agent under the `hermes` visa
  (default: work_private, project_shared, public). Adjust with
  `MEMORYD_VISAS` on the daemon.

`hermes memoryd status` reports daemon reachability plus incoming,
processing, dead-letter, and durability-fault state. It exits unhealthy when
the daemon is unreachable or durable evidence needs attention. The spool is
already inside the active `$HERMES_HOME`, so Hermes backup includes it and the
provider correctly returns no additional external `backup_paths`.

On POSIX, publication and every directory-entry change are protected with
file and directory fsyncs. On Windows, job files are fsynced, cross-process
claims use `msvcrt`, and atomic publications/moves use Win32
`MOVEFILE_WRITE_THROUGH`; Windows does not expose the same directory-fsync
primitive, so the status command labels power-loss metadata durability as
best-effort there. Process-crash recovery remains atomic on both platforms.

The manual `hermes memoryd miss` command uses this same disk spool and prints
its queued request id; it exits nonzero if durable publication fails.

## Tools exposed to the model

- `memoryd_search(query)` — on-demand deep recall beyond the injected packet.
- `memoryd_report_miss(detail)` — log "you forgot" signals to improve retrieval.

## Extraction model (which LLM distills memories)

Extraction runs in the **daemon**, not in Hermes — configure it there
(`MEMORYD_LLM=openrouter` + `OPENROUTER_API_KEY` + `MEMORYD_LLM_MODEL`
in `~/memory/config.json`'s `env` map lets you pick any vendor's model).
The pragmatic "same model as Hermes" setup today is pointing both at the
same OpenRouter key/model.

Planned keyless path (once Hermes exposes its model to plugins): the daemon
renders the extraction prompt, this plugin runs it through the Hermes-selected
host model, and posts the completion back for the daemon's validator to
judge — the validator stays server-side because it is the integrity layer.
Requires two small daemon endpoints (prompt render + completion submit); not
built until the Hermes plugin API exists to call against.
