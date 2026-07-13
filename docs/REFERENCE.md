# memoryd — full reference

Implements **M1–M5** of `slice_architecture_v1.md` plus a **Hermes Agent adapter** (Tier-1 integration via Hermes's memory-provider plugin system): canonical schema
with integrity firebreaks, content-addressed raw archive, append-only event
ledger, deterministic Claude Code hook loop, and a working recall pipeline
(hot + FTS + warning lane + rule-based court + lane-budgeted packet).

**Not yet built (by design — see spec §8):** remaining M6–M7 items: seeded
canary fixtures for live use, the S2 pairwise-rating harness, and the frozen
scenario repo. (Court rules, canary catching, S12 rebuild, and miss logging
already exist and are tested.)

## What is verified

- Ledger is append-only (UPDATE/DELETE rejected by trigger)
- Ariadne rule: a memory cannot become `active` without a source event
- Active memory text is immutable (supersede, don't edit)
- Supersession auto-marks the old memory `superseded` and closes its validity
- Capture: transcript → content-addressed blob + ledger events, idempotent re-ingestion
- Recall: directives/warnings always in Lane 1; hot set in Lane 2; FTS precision
  channel over extracted terms; packet cites `mem_` ids; <700 ms
- Canary memories are caught by the court (never surface) **and** raise an alarm
- Bitter-Lesson extension points are DB-free tested: model profiles,
  extraction contracts, recall policies, adapter envelopes, source packing,
  static eval, admin endpoint registration, and migration shape

## Setup (Windows/macOS/Linux)

```bash
pip install git+https://github.com/chrisduvillard/memoryd
memoryd install     # Docker pgvector DB + all migrations + hooks + autostart
memoryd status      # verify
python scripts/smoke_test.py    # expects daemon running; 19/19 must pass
```

Fresh installer-managed Docker databases use a random high-entropy password.
The password-bearing DSN is persisted to the config file (mode `0600` on
POSIX) and is masked in command output. A working existing DSN is adopted;
legacy `memoryd-pgvector` containers using password `memoryd` remain
adoptable without data deletion. Fresh credentials are atomically persisted to
owner-only `~/memory/.managed-postgres.json` before Docker creation so a crash
before migration/config publication remains recoverable. This secret record is
not part of a backup snapshot. If the container is absent but the named volume
remains, its recorded credential is reused. Without a record, a read-only,
networkless ephemeral probe checks whether `PG_VERSION` is nonempty: empty data
is treated as fresh, initialized data attempts only the legacy `memoryd`
credential and persists it only after readiness proves it, and an inconclusive
probe refuses without mutation. Docker receives these values through a
short-lived owner-only env file, not argv. A failed or timed-out Docker command
deletes a newly generated record only after follow-up inspection proves that
both container and volume are absent.

Manual path (bring your own Postgres 16 + pgvector; no Docker):

```bash
# 1. Database (applies ALL migrations)
MEMORYD_DB=memoryd MEMORYD_ROLE=$(whoami) ./scripts/init_db.sh

# 2. Daemon
pip install -r requirements.txt
export MEMORYD_DSN="postgresql://$(whoami)@/memoryd?host=/var/run/postgresql"  # peer auth
export MEMORYD_HOME="$HOME/memory"
memoryd serve                    # or: python3 -m memoryd serve

# 3. Hooks: merge hooks/settings.snippet.json into ~/.claude/settings.json
#    (replace <PYTHON> with the interpreter that has memoryd installed)
```

**Config precedence** (everywhere): environment variable > `~/memory/config.json`
(written by `memoryd install`; keys `dsn`, `port`, `home`, `visas`, plus an
`env` map applied via setdefault — how autostarted daemons get e.g.
`ANTHROPIC_API_KEY`) > built-in default. The config file's *location* honors
only the `MEMORYD_HOME` env var.

## Layout

```
migrations/001_init.sql   canonical schema + firebreak triggers (spec §3)
memoryd/core.py           config, ULIDs, episodic barcodes, archive, ledger
memoryd/ingest.py         Claude Code transcript JSONL → archive + events
memoryd/recall.py         hot/FTS/warning channels, court rules, lane budgets
memoryd/server.py         stdlib HTTP daemon: /recall /capture /capture-events
                          /extract /miss /health /admin/rebuild-indexes
memoryd/hook.py           Claude Code hooks (stdlib, cross-platform):
                          recall → inject packet (fail-open, visible marker);
                          capture → POST /capture (spool on failure)
memoryd/model_gateway.py  model profiles + capability metadata
memoryd/contracts.py      versioned extraction contracts
memoryd/semantic_policies.py semantic validation + promotion policies
memoryd/policies.py       versioned recall policies + packet compilers
memoryd/source_pack.py    deterministic source packing for extraction/replay
memoryd/evaluator.py      static eval core used by admin + microsleep
memoryd/cli.py            install|status|serve|review|microsleep|backup|uninstall
memoryd/backup.py         offline create/list/verify/restore snapshots
memoryd/doctor.py         read-only integrity inspection + conservative repair
scripts/init_db.sh        role + db + ALL migrations (manual/psql path)
scripts/smoke_test.py     the DB-backed verification suite
```

## Offline backup and restore

```text
memoryd backup create [--output PATH] [--retain 14]
memoryd backup list [--output PATH]
memoryd backup verify SNAPSHOT
memoryd backup restore SNAPSHOT --dsn TARGET_DSN --home TARGET_HOME
```

The default output is `~/memory/backups`; generated snapshot directories are
named `<UTC compact>-v1`. `create` refuses while the daemon answers its health
endpoint and runs the read-only spool/archive doctor checks before writing.
Each owner-only snapshot contains `database.dump` (PostgreSQL custom format),
`memory.tar.gz` (`archive/` and `spool/` only), `config.sanitized.json`, and a
checksummed v1 manifest. API keys and password-bearing DSNs are excluded. The
manifest records the secret environment-variable names that must be re-entered
and the actual applied filenames queried from the database's
`schema_migrations` ledger.
POSIX creation, verification, and restore require snapshot/home directories to
be mode `0700` and files to be `0600`; permission-setting failures abort.
Windows chmod protection is best-effort, so operators should also use
account-private NTFS ACLs.

`verify` checks the exact file allowlist, sizes and SHA-256 checksums, database
dump signature, JSON schema, and every tar member. Absolute/traversal paths,
links, devices, and other special tar entries are rejected. `list` performs the
same checks without mutation. Retention runs only after a newly created
snapshot verifies and removes only older valid generated snapshot directories;
it never follows symlinks or removes unrecognized/corrupt paths.

Restore is deliberately out-of-place. Stop the daemon and provide an empty
target PostgreSQL database plus a new, nonsymlink target home. POSIX also
accepts an existing empty target directory and atomically replaces it. Windows
requires the target path to be absent because replacing an existing directory
is not atomic there. The command verifies first, stages beside the target, then
restores with `pg_restore` using `--exit-on-error`, `--single-transaction`,
`--no-owner`, and `--no-privileges`. It writes a config containing the target
DSN/home and no API keys, then atomically publishes the home. If `pg_restore`
fails, the snapshot remains intact and the target transaction rolls back. A
restore drill should finish with `MEMORYD_HOME=<target> memoryd doctor`;
re-enter API keys separately.

Local `pg_dump`/`pg_restore` calls receive connection secrets through a private,
per-operation libpq service file under owner-only
`$MEMORYD_HOME/.pg-service/` and use only `service=memoryd` on argv. Cleanup is
fsynced and retried. An owner-only durable OS lock serializes the stale sweep,
service-file lifetime, database tool, and cleanup across processes; a crash
releases the lock so a later operation can safely reclaim validated residue.
Docker fallback transfers use an unpredictable remote path per operation and
remove only that operation's path, including after a partial copy failure.

Backups are local-only: memoryd does not upload snapshots. Linux installs a
02:35 persistent systemd user timer that stops `memoryd.service`, creates and
verifies a retained snapshot, and restarts the daemon even after failure.
Windows and macOS do not schedule backups automatically.

## Durable capture and recovery

The Claude Code capture hook sends the transcript path to the daemon first. It
snapshots transcript bytes into the local spool only after daemon delivery
fails. If delivery and local spooling both fail, the hook stays fail-open and
prints a visible evidence-loss warning.

The daemon persists work before acknowledging it. `/capture` copies and hashes
the transcript, then writes its capture manifest. `/extract` writes an
extraction manifest. Each healthy endpoint then returns HTTP 202 with exactly
`{"queued": true}`.

The spool uses this layout:

```text
~/memory/spool/
  blobs/<sha256>       content-addressed transcript snapshots
  incoming/*.json     queued jobs; legacy flat *.json jobs also remain readable
  processing/*.json   claimed jobs with an mtime-based lease
  dead-letter/*.json  preserved jobs and their reason records
  state.lock           persistent cross-process state-transition lock
```

Schema-v2 jobs share `schema_version`, `job_id`, `kind`, `created_at`,
`session_id`, `attempts`, `last_error`, and `next_attempt_at`. A
`capture_snapshot` job also records `trigger`, `original_transcript_path`,
`blob_sha256`, `blob_bytes`, and optional `project`. An `extraction` job needs
only the shared fields. Every accepted capture gets a distinct job. Several
jobs and repeated attempts can still deduplicate to the same blob bytes.
Blob and manifest publication syncs every newly created parent entry and the
containing directory where the platform permits it. Known platform limitations
disable directory sync; genuine I/O failures abort acknowledgement. A process
discards a duplicate temporary blob only after
the incumbent is a regular file with the expected size and digest. If that
check fails, memoryd preserves the known-good bytes separately and refuses the
capture instead of acknowledging untrusted evidence.

Claims move jobs from `incoming/` to `processing/` under `state.lock` and touch
and file-sync the manifest mtime. Each cross-directory state move syncs the
destination before the source directory; completion syncs the processing
directory after unlink. A worker requeues a processing lease older than 15 minutes.
Rename-race handling covers only the rename syscall. If a post-rename sync or
lease touch fails, the error propagates with the manifest already in its new
state; it is never misreported as a missing-source no-op.
Transient failures increment `attempts`, record `last_error`, set an
exponential `next_attempt_at`, and return the job to `incoming/`. Permanent
validation failures move the manifest to `dead-letter/`.

A dead-letter transition preserves the manifest and writes its derived reason
record. Code recognizes a reason sidecar only when its JSON object contains
exactly `dead_lettered_at`, `reason`, and `manifest`; `manifest` names an
existing sibling file; and the sidecar path exactly matches
`dead_letter_reason_path(manifest)`. A `.reason.json` suffix alone proves
nothing. Malformed or unreadable ambiguity counts as evidence instead of
hiding a job.

Transcript parsing gives each ledger event a stable source identity. It uses
the transcript's native UUID when present; otherwise, it combines the line
number with the raw-line SHA-256. It then appends the block ordinal and event
kind. Replaying the same transcript therefore does not duplicate ledger rows.
A mixed content line emits each supported text, tool-call, and tool-result
block in order instead of dropping all but one block.
Malformed or unsupported JSON values, including non-string text and tool
results, stay in the raw archive but emit no ledger event. Spool replay reads
and verifies the blob through one file
descriptor, then passes those exact bytes to archival and classification.

The archive stores verified objects at
`archive/objects/sha256/<first-2>/<next-2>/<sha256>`. Before use, memoryd checks
that an object is a regular file with the expected size and digest.
Every successful archive attempt appends an occurrence to
`archive/manifest.jsonl`, including its `ingest_job_id` when available. A retry
can append another occurrence with the same job ID even when the object bytes
already exist. The `archive/fonds/` view is a platform-safe, best-effort
symlink view. Unsupported platforms or a link failure do not invalidate the
verified object or occurrence manifest.
Archive publication keeps its fsynced temporary inode and an open verified
canonical descriptor through manifest-lock preconditions before and after the
append. A leaf replacement rolls back the occurrence and retains the temporary
file as evidence. Successful publication removes and directory-syncs the
temporary name only after the occurrence is durable.
Unrelated failures, such as manifest-lock timeout or I/O while the canonical
leaf remains verified, remove the temporary file. Fonds paths are validated
before object bytes are published, preventing invalid-path temp accumulation.
Capture fonds dates come from the job's UTC `created_at`, so a retry after
midnight retains the same path. Repair derives the path from the same field and
rechecks occurrence identity while holding the manifest append lock. Fonds
construction normalizes session backslashes to slashes before archival and
repair compare identities.

### Status and operator action

`memoryd status` reports `incoming`, `processing`, and `dead-letter`. Incoming
includes legacy flat jobs and `incoming/` jobs. Dead-letter excludes only exact
structural reason sidecars. Any dead-letter evidence marks status unhealthy and
points the operator to `memoryd doctor`.

Run `memoryd doctor` first. It reads spool and archive topology, jobs, blob
digests, leases, sidecars, objects, occurrence manifests, database health, and
daemon health without creating or changing files. Inspect each dead-letter
manifest with its matching reason record. Restore missing bytes from a trusted
source or backup outside memoryd, then queue only a validated copy after fixing
the cause. Keep the dead-letter evidence.

`memoryd doctor --repair` performs only conservative actions. It creates a
missing safe spool layout, requeues stale processing leases, upgrades legacy
jobs whose transcript source still exists, and moves invalid or unrecoverable
queued manifests to dead-letter with a reason record. It reconstructs missing
supported capture-job occurrences idempotently, only from existing spool
evidence and an existing canonical object that passes regular-file, size,
digest, and file-identity checks. Repair refuses redirected, unreadable, or
otherwise untrusted spool/archive topology,
including unsafe ancestors, locks, evidence files, blobs, and object shards.
It never recreates missing bytes, deletes evidence, rewrites archive objects, or
manufactures fonds links.

`memoryd doctor` reports hidden `.collision.*` and `.job_*.tmp` spool files as
`unmanifested_capture_evidence`. These files contain bytes preserved after a
publication failure but have no replay manifest. Retain and review them; repair
never deletes them automatically.

## Operating notes

- **Fail-open** (P9): if the daemon is down at recall, the agent proceeds with a
  visible `[memory: unavailable this turn]` marker. After a capture delivery
  failure, the hook snapshots readable transcript bytes into
  `~/memory/spool/`; the daemon drains that spool after recovery.
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
  claude-haiku-4-5-20251001), `openrouter` (default when only
  OPENROUTER_API_KEY is set; any vendor's model via MEMORYD_LLM_MODEL slugs,
  default `google/gemini-3.5-flash` — validator-benchmarked), `openai` (any OpenAI-compatible
  /chat/completions — set MEMORYD_LLM_BASE, e.g.
  `http://localhost:11434/v1` for keyless local Ollama), or `mock` for
  tests. No key -> capture-only mode; extraction backfills later via
  micro-sleep retry.
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

## Bitter-Lesson extension layer (migration 005)

The v1 safety substrate stays deterministic, but the model-facing parts are now
versioned and replayable instead of frozen in code:

- `memoryd/model_gateway.py` exposes model profiles and capabilities via
  `MEMORYD_MODEL_PROFILE` while preserving the old `MEMORYD_LLM*` defaults.
- `memoryd/contracts.py` holds extraction contracts such as `builtin_v1` and
  `wide_context_v1`; extraction records `model_runs` when migration 005 exists.
- `memoryd/semantic_policies.py` holds semantic validation and promotion
  policies. `conservative_v1` reproduces the current hedge-preservation and
  auto-promotion behavior.
- `memoryd/policies.py` holds recall policies and packet compiler profiles.
  The default `heuristic_v1` reproduces the previous fixed lanes and rerank
  weights; `oracle_v1` exists for eval comparisons.
- `memoryd/source_pack.py` renders deterministic extraction source packets and
  can pull archived blobs for wide-context contracts.
- `memoryd/adapters.py` defines the stable adapter envelope used by direct event
  ingestion so future runtimes do not define memory semantics at the edge.
- `memoryd/evaluator.py` powers `/admin/eval` and nightly micro-sleep evals.

New config keys: `MEMORYD_MODEL_PROFILE`, `MEMORYD_EXTRACTOR_CONTRACT`,
`MEMORYD_SEMANTIC_POLICY`, `MEMORYD_RECALL_POLICY`, `MEMORYD_PACKET_COMPILER`, and
`MEMORYD_EVAL_PROFILE`. Defaults reproduce current behavior.

New evidence/admin endpoints:

```text
POST /admin/model-profiles   list model profiles and capabilities
POST /admin/policies         list recall policies, packet compilers, contracts
POST /admin/eval             run static eval cases and record eval_runs
POST /admin/replay           return recent recall_log replay material
POST /admin/export-evidence  export recent model/policy/eval/packet runs
```

New tables: `memory_type_registry`, `event_type_registry`, `model_runs`,
`policy_runs`, `eval_cases`, `eval_runs`, and `packet_runs`. Migration 005 also
opens `memories.type` to text and removes the closed `events.kind` constraint;
the current built-in ontology is seeded into registries instead of being a DB
wall.

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
  inline; synchronously fsynced to the profile-scoped disk spool before the
  hook returns, then retried with persisted backoff while the daemon is down.
- **on_pre_compress** — snapshot captured BEFORE Hermes compresses context
  (the PreCompact equivalent).
- **on_memory_write** — built-in MEMORY.md/USER.md writes mirrored to
  canonical: vendor memory stays a cache, observed.
- **on_delegation** — subagent task/result pairs recorded on the parent.
- **on_session_end / on_session_switch(reset)** — flush + `/extract`.
- **Subagent/cron contexts never write** (agent_context != 'primary').
- Tools exposed to the model: `memoryd_search`, `memoryd_report_miss`.
- CLI: `hermes memoryd status|config|miss`.

Setup: export the authoritative `HERMES_HOME`, then copy
`hermes_plugin/memoryd/` to `$HERMES_HOME/plugins/memoryd/` (or let
`memoryd install` do so). Activate with
`hermes config set memory.provider memoryd` (or `hermes memory setup`).
Requires migrations 001–007. Per-agent scopes via `MEMORYD_VISAS`, e.g.
`{"hermes": ["work_private","project_shared","public"]}`. Cross-agent
sharing is automatic: memories extracted from Claude Code sessions are
recallable in Hermes (and vice versa) under each agent's visa.

Version note: the plugin targets Hermes Agent tag `v2026.6.5`, resolved commit
`3c231eb3979ab9c57d5cd6d02f1d577a3b718b43`, source path
`agent/memory_provider.py`. The vendored pinned contract is
`scripts/_stubs/agent/memory_provider.py`.

Hermes moves fast (`hermes update`). If plugin activation fails after an
update, run this exact DB-free compatibility check against the Hermes checkout
you installed or updated:

```bash
python scripts/check_hermes_contract.py --source-root /path/to/hermes-agent
```

Without `--source-root`, the checker validates the actually installed
`agent.memory_provider.MemoryProvider` source found on `sys.path`. Source
contracts are parsed statically with Python's AST and are never imported or
executed; plugin instantiation always uses the trusted vendored snapshot. A
mismatch report identifies removed methods, abstract-method changes, and
required signature changes. The pinned CI gate also passes
`--require-pinned-bytes` to require exact source identity.

## Next: deploy + trial (M6–M8)

Seed 10 canary memories, wire the nightly cron, run one real month (now
with BOTH agents on the substrate), rate S2 weekly, then write the
D1/D2/D4 decision memo.
