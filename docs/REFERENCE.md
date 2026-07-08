# memoryd — full reference

Implements **M1–M5** of `slice_architecture_v1.md` plus a **Hermes Agent adapter** (Tier-1 integration via Hermes's memory-provider plugin system): canonical schema
with integrity firebreaks, content-addressed raw archive, append-only event
ledger, deterministic Claude Code hook loop, and a working recall pipeline
(hot + FTS + warning lane + rule-based court + lane-budgeted packet).

**Not yet built (by design — see spec §8):** remaining M6–M7 items: seeded
canary fixtures for live use, the S2 pairwise-rating harness, and the frozen
scenario repo. (Court rules, canary catching, S12 rebuild, and miss logging
already exist and are tested.)

## What is verified (73 checks: smoke 19 + extract 18 + vector/S12 13 + hermes 23)

- Ledger is append-only (UPDATE/DELETE rejected by trigger)
- Ariadne rule: a memory cannot become `active` without a source event
- Active memory text is immutable (supersede, don't edit)
- Supersession auto-marks the old memory `superseded` and closes its validity
- Capture: transcript → content-addressed blob + ledger events, idempotent re-ingestion
- Recall: directives/warnings always in Lane 1; hot set in Lane 2; FTS precision
  channel over extracted terms; packet cites `mem_` ids; <700 ms
- Canary memories are caught by the court (never surface) **and** raise an alarm

## Setup (Linux/macOS, Postgres 16)

```bash
# 1. Postgres 16 + pgvector (extension only needed from M5, but schema expects it)
#    Ubuntu: apt install postgresql-16; build pgvector from github.com/pgvector/pgvector
# 2. Database
MEMORYD_DB=memoryd MEMORYD_ROLE=$(whoami) ./scripts/init_db.sh
psql -d memoryd -f migrations/002_extraction.sql
psql -d memoryd -f migrations/003_multi_agent.sql

# 3. Daemon
pip install -r requirements.txt
export MEMORYD_DSN="postgresql://$(whoami)@/memoryd?host=/var/run/postgresql"  # peer auth
export MEMORYD_HOME="$HOME/memory"
python3 -m memoryd.server        # run under systemd/launchd for real use

# 4. Hooks
mkdir -p ~/memory/hooks && cp hooks/*.sh ~/memory/hooks/
# merge hooks/settings.snippet.json into ~/.claude/settings.json

# 5. Verify
python3 scripts/smoke_test.py    # expects daemon running; 19/19 must pass
```

## Layout

```
migrations/001_init.sql   canonical schema + firebreak triggers (spec §3)
memoryd/core.py           config, ULIDs, episodic barcodes, archive, ledger
memoryd/ingest.py         Claude Code transcript JSONL → archive + events
memoryd/recall.py         hot/FTS/warning channels, court rules, lane budgets
memoryd/server.py         stdlib HTTP daemon: /recall /capture /miss /health /admin
hooks/recall_hook.sh      UserPromptSubmit → inject packet (fail-open, visible marker)
hooks/capture_hook.sh     Stop/SessionEnd/PreCompact → capture (spool on failure)
scripts/init_db.sh        role + db + migration
scripts/smoke_test.py     the 19-check verification suite
```

## Operating notes

- **Fail-open** (P9): if the daemon is down at recall, the agent proceeds with a
  visible `[memory: unavailable this turn]` marker; capture failures spool to
  `~/memory/spool/` and drain on daemon start. Raw evidence is never lost.
- **Meta events** (`recall_packet`, `capture_ack`) carry `meta=true` and are
  excluded from future extraction — the regress firebreak.
- **Canary alarms** write a `veto` event to the ledger; check
  `SELECT * FROM events WHERE kind='veto'` — any row is a scoping leak to fix.
- The daemon binds `127.0.0.1` only. Do not expose it.

## M3: extraction, promotion, review, micro-sleep

- `memoryd/extract.py` — one LLM call per session (spec P7), then a
  deterministic validator that does NOT trust the model: source ids must be
  real; A1 requires a verbatim evidence quote (else auto-downgrade to A2);
  hedged sources + committal candidate text -> chaperone hold; directive/
  warning structs mandatory. Verified paths (test_extract.py): auto-promotion
  of A1 directives, hedge preservation, fake-source rejection, identity
  gating behind user confirmation, dedup-as-affirmation, contradiction ->
  review (never auto-supersede), idempotent re-runs.
- `memoryd/llm.py` — provider abstraction. `MEMORYD_LLM=anthropic` (default
  when ANTHROPIC_API_KEY is set; model via MEMORYD_LLM_MODEL, default
  claude-haiku-4-5-20251001) or `mock` for tests. No key -> capture-only
  mode; extraction backfills later via micro-sleep retry.
- `memoryd/review.py` — human control plane CLI:
  `queue | mem <id> | approve <qid> | reject <qid> | confirm <id> | supersede <old> <new>`.
  Approving a contradiction promotes the new memory and supersedes the old.
- `memoryd/microsleep.py` — nightly cron: drain spool, retry missed
  extractions, expire priming, decay unrecalled candidates, write a daily
  digest to ~/memory/digest/ (pending reviews, canary/veto alarms, miss
  signals, recall latency).

## M5: hybrid retrieval (vector channel + rerank)

- `memoryd/embed.py` — pluggable embedders via `MEMORYD_EMBED`:
  `voyage` (VOYAGE_API_KEY), `openai` (OpenAI-compatible /v1/embeddings —
  also Ollama/LM Studio/OpenRouter via MEMORYD_EMBED_BASE), or `hash`
  (default: built-in deterministic feature-hash embedder, zero deps,
  offline). All vectors normalized to 1024 dims, so switching providers is
  an index rebuild, never a schema change.
- Recall now merges FTS (precision) + vector (recall) with the spec §6.4
  rerank: 0.35·semantic + 0.20·keyword + 0.15·recency + 0.15·useful +
  0.10·authority + 0.05·confirmation-recency. If embedding fails, recall
  degrades to FTS-only and logs the missing channel.
- Verified: a memory with zero FTS-lexeme overlap with the query is still
  retrieved through the vector channel; S12 holds (drop all embeddings ->
  /admin/rebuild-indexes -> frozen-query packets byte-identical).
- Embeddings are written at extraction time, backfilled by micro-sleep, and
  fully regenerated by /admin/rebuild-indexes — three paths, one view.

**Embedder choice:** `hash` proves the pipeline and handles lexical/morpho
overlap, but it is NOT semantic — paraphrases with disjoint vocabulary will
miss. For the trial month set `MEMORYD_EMBED=voyage` or point `openai` at a
local Ollama (e.g. nomic-embed-text) to stay fully local.

## Hermes Agent integration (hermes_plugin/memoryd/)

Hermes (NousResearch hermes-agent) has a first-class memory-provider plugin
system whose runtime calls prefetch-before-every-turn and sync-after-every-
turn — i.e. deterministic recall/capture, our Tier-1 requirement, natively.
The plugin implements the upstream `MemoryProvider` ABC (tested against the
verbatim ABC from the Hermes repo, vendored in scripts/_stubs/):

- **prefetch/queue_prefetch** — per-turn packet from `/recall` under the
  `hermes` visa; background-cached; first-turn bounded sync call; fail-open
  with one visible marker.
- **sync_turn** — every turn to the ledger via `/capture-events` (agent=
  'hermes'); oversize text auto-archived content-addressed + truncated
  inline; in-memory spool (500) while the daemon is down, flushed on
  recovery.
- **on_pre_compress** — snapshot captured BEFORE Hermes compresses context
  (the PreCompact equivalent).
- **on_memory_write** — built-in MEMORY.md/USER.md writes mirrored to
  canonical: vendor memory stays a cache, observed.
- **on_delegation** — subagent task/result pairs recorded on the parent.
- **on_session_end / on_session_switch(reset)** — flush + `/extract`.
- **Subagent/cron contexts never write** (agent_context != 'primary').
- Tools exposed to the model: `memoryd_search`, `memoryd_report_miss`.
- CLI: `hermes memoryd status|config|miss`.

Setup: copy `hermes_plugin/memoryd/` to `<hermes>/plugins/memory/memoryd/`,
then `hermes config set memory.provider memoryd` (or `hermes memory setup`).
Requires migrations 001–003. Per-agent scopes via `MEMORYD_VISAS`, e.g.
`{"hermes": ["work_private","project_shared","public"]}`. Cross-agent
sharing is automatic: memories extracted from Claude Code sessions are
recallable in Hermes (and vice versa) under each agent's visa.

Version note: the plugin targets the MemoryProvider ABC as of hermes-agent
main, 2026-07. Hermes moves fast (`hermes update`); if activation fails
after an update, re-diff `agent/memory_provider.py` against
`scripts/_stubs/agent/memory_provider.py` — divergence there is the first
thing to check.

## Next: deploy + trial (M6–M8)

Seed 10 canary memories, wire the nightly cron, run one real month (now
with BOTH agents on the substrate), rate S2 weekly, then write the
D1/D2/D4 decision memo.
