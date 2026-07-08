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

- **Fail-open.** Daemon down → Hermes proceeds; one visible
  `[memoryd: unavailable]` marker; turns spool in memory (500 cap) and
  flush on recovery. Nothing ever blocks the agent.
- **Non-primary contexts never write.** Cron/subagent/flush contexts are
  read-only toward memory — a cron prompt can't corrupt user memory.
  Subagent work still reaches the ledger via the parent's `on_delegation`.
- **Built-in MEMORY.md stays active** (Hermes always keeps it). This
  provider treats it as a vendor cache: writes are mirrored to canonical
  via `on_memory_write`, so nothing lives only in the vendor layer.
- **Visa scoping.** The daemon serves this agent under the `hermes` visa
  (default: work_private, project_shared, public). Adjust with
  `MEMORYD_VISAS` on the daemon.

## Tools exposed to the model

- `memoryd_search(query)` — on-demand deep recall beyond the injected packet.
- `memoryd_report_miss(detail)` — log "you forgot" signals to improve retrieval.
