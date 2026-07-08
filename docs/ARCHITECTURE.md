# Memory System — Vertical Slice Architecture v1

**Phase:** Exploitation (exploration directive expired 2026-07-07 by explicit user instruction).
**Scope:** The thin vertical slice only. Everything else stays in the research report.
**Goal of the slice:** Produce *evidence* that resolves D1 (curated layer shape), D2 (enforcement mechanism), and D4 (promotion aggressiveness) — while being a genuinely useful memory system for one agent (Claude Code) on real data from day one.

---

## 0. Scope contract

**IN the slice (8 components):**

1. Raw Archive (content-addressed file store)
2. Event Ledger (append-only, the WAL)
3. Canonical memory objects (dual-body: envelope + text) with supersession + treatment
4. Minimal Authority Office (entity table, conservative merging)
5. Hook loop on Claude Code (deterministic recall + capture)
6. Retrieval pipeline: hot + keyword + vector + warning lane + rule-based court + budgeted packet
7. Capture pipeline: raw always; extraction per session; quorum-lite promotion
8. Instrumentation: the 5 test suites (S2, S3, S6, S7, S12) + miss logging

**OUT (explicitly deferred, do not build even if tempting):**
Zettelkasten UI/vault sync, temporal graph store, multi-agent exchange, LLM-judge court, Memory Palace UI, dialect compilers beyond the Claude packet, deep-sleep consolidation, Physarum routing, Context Auctioneer beyond fixed lanes, MCP server (see §2 rationale), vendor-memory sync.

**Slice success criteria (measured, not vibes):**
- S2: memory-on beats memory-off in blind pairwise comparison on ≥60% of real tasks, loses on <10%.
- S3: 100% correct supersession resolution on the regression set.
- S6: veto fires on all seeded repeat-failure scenarios; false-veto rate <5%.
- S7: zero canary surfacings, zero sealed-scope leaks.
- S12: full index drop + rebuild produces identical packets on a frozen query set.
- Recall injection adds <700ms p95 to turn start.

---

## 1. Provisional decisions (slice-only, reversible, labeled)

These are *not* the final architecture answers. They are the cheapest defensible choices that let the slice generate evidence. Each is tagged with the decision it feeds.

| # | Provisional choice | Feeds | Rationale / reversal path |
|---|---|---|---|
| P1 | **Postgres 16 + pgvector** as the one datastore (events, memories, entities, FTS, vectors) | D3, D6 | The archive's own candidate first stack. One backup story, one query surface. Raw archive lives on filesystem, DB holds pointers. Reversal: everything below is rebuildable from ledger+archive by construction. |
| P2 | **Raw archive on local filesystem**, content-addressed (`sha256`), under `~/memory/archive/` | D3, D9 | Local-first sovereignty; object storage is a later swap behind the same address scheme. |
| P3 | **Memory objects are the canonical curated layer; no separate zettel files, no graph store** | **D1** | The slice tests whether a single dual-body object (structured envelope + markdown text) serves retrieval. If timeline/relationship queries hurt without a graph, that *is* the D1 evidence. |
| P4 | **Hooks only; no MCP server in the slice** | **D2** | The mandatory path is what we're testing. Adding MCP now would contaminate the measurement of hook reliability. MCP is additive later. |
| P5 | **Court is rule-based only** (scope, validity, activation-condition, directive checks) | D5 | Per-turn latency budget is tight; rules are deterministic and testable. Ambiguity cases get *logged* to build the labeled set that later decides rules vs. LLM-judge. |
| P6 | **Promotion is stingy** (quorum-lite, §5.3); everything else stays `candidate` | **D4** | We measure what stinginess costs (misses) vs. saves (pollution). Easier to loosen than to clean up. |
| P7 | **Extraction runs once per session** (on Stop/SessionEnd), not per turn | D7 | One LLM call per session bounds cost. Per-turn capture is raw-only and free. |
| P8 | **Packet budget: 1,500 tokens** with fixed lanes (§6.4) | D8 | Fixed lanes now; auction later. Budget is a config constant, tuned by S2. |
| P9 | **Fail-open recall** (daemon down → agent proceeds, turn stamped `memory_unavailable`) | — | A personal assistant that refuses to work when its memory daemon hiccups is worse than one that visibly proceeds without memory. Fail-closed is one config flag away for sensitive contexts. |

---

## 2. Component topology

```
┌─────────────────────────────────────────────────────────────┐
│ Claude Code (the only tenant in the slice)                   │
│                                                              │
│  UserPromptSubmit hook ──► recall_hook.sh ──┐                │
│  PreCompact hook ─────────► capture_hook.sh ├──► memoryd     │
│  Stop / SessionEnd hook ──► capture_hook.sh ┘   (localhost)  │
└─────────────────────────────────────────────┬────────────────┘
                                              │ HTTP over 127.0.0.1
┌─────────────────────────────────────────────▼────────────────┐
│ memoryd  (single long-running daemon, systemd/launchd)       │
│                                                              │
│  /recall   sync, <700ms budget, serves the packet            │
│  /capture  async ack-fast, queues ingestion                  │
│  /admin    promotion queue, test harness, rebuild            │
│                                                              │
│  In-RAM: hot memory set, compiled directive/warning tables   │
│  Workers: ingestor, extractor (session-end), micro-sleep     │
└──────────────┬───────────────────────────────┬───────────────┘
               │                               │
     ┌─────────▼─────────┐          ┌──────────▼──────────┐
     │ Postgres+pgvector │          │ ~/memory/archive/    │
     │ ledger, memories, │          │ content-addressed    │
     │ entities, FTS,    │◄─pointers│ raw objects          │
     │ embeddings, logs  │          │ (transcripts, tools) │
     └───────────────────┘          └─────────────────────┘
```

Why a daemon and not per-invocation scripts: recall must hit <700ms including an embedding call; a warm process with the hot set in RAM, a pooled DB connection, and a cached embedding client is the only realistic way. Hooks stay dumb (`curl` + `jq`), so hook fragility never carries logic.

---

## 3. Canonical schema (first, because hardest to change)

Design rules baked into the DDL:
- The **ledger is the WAL**; every other table is a materialized view over it and must be rebuildable (`S12` enforces this).
- **No `UPDATE` on truth.** Events are insert-only. Memories change status via supersession rows and treatment rows, never destructive edits to `text`.
- **Every derived row cites its sources** or it cannot reach `active`.

### 3.1 Raw archive (filesystem)

```
~/memory/archive/
  objects/sha256/ab/cd/abcd…       # immutable blobs (transcripts, tool outputs, files)
  fonds/claude-code/2026/07/07/    # symlinks preserving origin + original order
manifest.jsonl                     # append-only: {sha256, bytes, mime, first_seen, fonds_path}
```

Content-addressing gives dedup and tamper-evidence for free (a blob's ID *is* its hash — Provenance-Notary-lite without a chain). Fonds symlinks preserve original order per the archival-science rule: archive is chronological, knowledge layer reorganizes pointers only.

### 3.2 Event ledger

```sql
CREATE TABLE events (
  id            TEXT PRIMARY KEY,              -- evt_<ulid>
  ts            TIMESTAMPTZ NOT NULL,
  kind          TEXT NOT NULL,                 -- user_message | agent_response | tool_call
                                               -- | tool_result | session_start | session_end
                                               -- | recall_packet | capture_ack | promotion
                                               -- | supersession | veto | miss_signal
  session_id    TEXT NOT NULL,
  agent         TEXT NOT NULL DEFAULT 'claude-code',
  project       TEXT,                          -- cwd-derived project slug
  raw_sha256    TEXT,                          -- pointer into archive (nullable for meta)
  payload       JSONB NOT NULL,                -- small structured summary; big stuff → archive
  meta          BOOLEAN NOT NULL DEFAULT FALSE,-- meta-events: archived, NOT re-consolidated
  barcode       TEXT NOT NULL                  -- episodic barcode: ts|session|kind|hash8
);
CREATE INDEX ON events (session_id, ts);
CREATE INDEX ON events (project, ts);
CREATE INDEX ON events (kind, ts) WHERE NOT meta;
```

The `meta` flag is the regress firebreak: recall packets and promotions are ledgered for audit but the extractor's default query is `WHERE NOT meta`.

### 3.3 Memory objects (the dual-body canonical unit)

```sql
CREATE TYPE mem_status AS ENUM ('candidate','active','quarantined','rejected','superseded');
CREATE TYPE mem_type   AS ENUM ('identity','preference','writing_style','project_state',
                                'decision','open_question','commitment','person','company',
                                'technical_fact','workflow','constraint','procedure',
                                'directive','warning','priming');

CREATE TABLE memories (
  id            TEXT PRIMARY KEY,              -- mem_<ulid>
  type          mem_type NOT NULL,
  text          TEXT NOT NULL,                 -- human body: one well-formed paragraph
  struct        JSONB NOT NULL DEFAULT '{}',   -- machine body: type-specific fields
                                               --   directive: {directive, condition, expires, severity}
                                               --   warning:   {class, target, evidence_refs, severity}
                                               --   decision:  {options, chosen, rationale}
                                               --   preference:{strength}
  project       TEXT,                          -- NULL = global
  scope         TEXT NOT NULL DEFAULT 'work_private',
  sensitivity   TEXT NOT NULL DEFAULT 'normal',-- public|normal|private|sealed
  authority     TEXT NOT NULL,                 -- A1|A2|B1|C1|D1|Q  (source reliability)
  confidence    REAL NOT NULL,
  status        mem_status NOT NULL DEFAULT 'candidate',
  valid_from    DATE NOT NULL,
  valid_to      DATE,                          -- NULL = open
  activation    JSONB NOT NULL DEFAULT '{}',   -- {task_type:[], audience:[], exclude:[]}
  half_life_d   INTEGER,                       -- NULL = no decay (identity tier)
  last_confirmed_at TIMESTAMPTZ,
  recall_count  INTEGER NOT NULL DEFAULT 0,
  useful_count  INTEGER NOT NULL DEFAULT 0,    -- feedback-attributed successes
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  is_canary     BOOLEAN NOT NULL DEFAULT FALSE -- S7 tripwires; must NEVER surface
);

CREATE TABLE memory_sources (                   -- Ariadne thread (N:M)
  memory_id TEXT REFERENCES memories(id),
  event_id  TEXT REFERENCES events(id),
  role      TEXT NOT NULL DEFAULT 'derived_from',
  PRIMARY KEY (memory_id, event_id)
);

CREATE TABLE supersessions (                    -- COW, never overwrite
  old_id    TEXT REFERENCES memories(id),
  new_id    TEXT REFERENCES memories(id),
  reason    TEXT,
  at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (old_id, new_id)
);

CREATE TABLE treatments (                       -- citator-lite
  id        BIGSERIAL PRIMARY KEY,
  memory_id TEXT REFERENCES memories(id),
  kind      TEXT NOT NULL,   -- affirmed|limited|distinguished|questioned
                             -- |confirmed_by_user|contradicted_by_user|stale
  by_ref    TEXT,            -- mem_ or evt_ that caused the treatment
  note      TEXT,
  at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Hard constraint enforced in the application layer (and by a DB trigger): **no row may enter `status='active'` with zero rows in `memory_sources`.** No source thread, no authority.

### 3.4 Entities (Authority Office, minimal)

```sql
CREATE TABLE entities (
  id         TEXT PRIMARY KEY,     -- person_alex, tool_hermes, org_acme…
  kind       TEXT NOT NULL,        -- person|org|tool|repo|location
  label      TEXT NOT NULL,
  aliases    TEXT[] NOT NULL DEFAULT '{}',
  do_not_merge_with TEXT[] NOT NULL DEFAULT '{}'
);
CREATE TABLE memory_entities (
  memory_id TEXT REFERENCES memories(id),
  entity_id TEXT REFERENCES entities(id),
  PRIMARY KEY (memory_id, entity_id)
);
```

Merge policy: **never auto-merge** (conservative per D10). Unresolvable mentions get a provisional `entity_pending_*` ID and a review-queue row.

### 3.5 Indexes (disposable by construction)

```sql
CREATE TABLE mem_embeddings (
  memory_id TEXT PRIMARY KEY REFERENCES memories(id),
  model     TEXT NOT NULL,
  embedding vector(1024) NOT NULL
);
CREATE INDEX ON mem_embeddings USING hnsw (embedding vector_cosine_ops);

ALTER TABLE memories ADD COLUMN fts tsvector
  GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED;
CREATE INDEX ON memories USING gin (fts);
```

`memoryd /admin/rebuild` drops and regenerates both from `memories` alone. S12 diffs frozen-query packets before/after. Embedding model name is stored per row so a model migration is a visible, partial-rebuild operation, not a mystery.

### 3.6 Feedback & miss log (instrumentation is schema, not an afterthought)

```sql
CREATE TABLE recall_log (
  id          BIGSERIAL PRIMARY KEY,
  ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
  session_id  TEXT, project TEXT,
  query_text  TEXT,
  packet      JSONB,          -- exactly what was injected (audit + S12 diffing)
  latency_ms  INTEGER,
  served      BOOLEAN         -- FALSE = fail-open occurred
);
CREATE TABLE miss_signals (
  id         BIGSERIAL PRIMARY KEY,
  ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
  session_id TEXT,
  signal     TEXT NOT NULL,   -- user_said_forgot | user_resupplied | answer_existed
  detail     JSONB
);
```

---

## 4. Hook loop (deterministic enforcement)

The hooks are the entire reason this architecture exists: recall and capture happen because the *runtime* fires them, never because the model decides to.

### 4.1 Claude Code hook wiring

```jsonc
// ~/.claude/settings.json (slice-relevant portion)
{
  "hooks": {
    "UserPromptSubmit": [{
      "hooks": [{ "type": "command",
                  "command": "~/memory/hooks/recall_hook.sh",
                  "timeout": 2 }]
    }],
    "Stop":        [{ "hooks": [{ "type": "command", "command": "~/memory/hooks/capture_hook.sh stop" }] }],
    "SessionEnd":  [{ "hooks": [{ "type": "command", "command": "~/memory/hooks/capture_hook.sh session_end" }] }],
    "PreCompact":  [{ "hooks": [{ "type": "command", "command": "~/memory/hooks/capture_hook.sh pre_compact" }] }]
  }
}
```

`PreCompact` matters more than it looks: compaction is where context (and therefore un-captured facts) silently dies. Capturing *before* compaction means nothing important depends on surviving it.

### 4.2 Recall hook (sync, budgeted)

```
recall_hook.sh (thin, no logic):
  1. read hook JSON from stdin (prompt, session_id, cwd, transcript_path)
  2. POST to http://127.0.0.1:7437/recall  (curl --max-time 1.5)
  3. on 200: emit {"hookSpecificOutput":{"hookEventName":"UserPromptSubmit",
                   "additionalContext": <packet markdown>}}
  4. on failure: emit additionalContext =
     "[memory: unavailable this turn — proceeding without recall]"
     and exit 0                                            # fail-open (P9)
```

The visible failure marker is deliberate: a silent fail-open would recreate the exact "did memory even run?" ambiguity the slice exists to kill. The marker also lets S2 exclude degraded turns.

`memoryd /recall` internal budget (p95 targets):

```
parse + task classify (regex/keyword heuristics)      ~5 ms
entity + project resolution (in-RAM alias map)         ~5 ms
hot set (in-RAM: identity, prefs, project_state,
         open commitments for this project)            ~1 ms
parallel: FTS query ──┐
          vector query ├─ (one embedding call ~150ms   ~200 ms
          warning scan ┘   dominates; FTS/scan ~10ms)
validity + activation + court rules + scope filter     ~5 ms
packet assembly + lane budgeting                       ~10 ms
                                            total p95 < 300 ms
```

If the embedding call is the tail risk, the fallback is: serve hot + FTS + warnings immediately at the 600ms deadline and drop the vector channel for that turn (logged in `recall_log.packet.channels`).

### 4.3 Capture hook (async, ack-fast)

```
capture_hook.sh <trigger>:
  1. read hook JSON (transcript_path, session_id, cwd)
  2. POST {trigger, transcript_path, session_id, project} to /capture
  3. daemon: 202 Accepted immediately; hook exits
```

`memoryd` ingestion queue then, per trigger:

```
always (any trigger):
  - copy transcript delta → archive (sha256), append manifest
  - ledger events for each new turn/tool call (kind, barcode, raw pointer)

on session_end | pre_compact (P7 — once per session):
  - run EXTRACTOR (single LLM call) over the session's non-meta events
  - candidates → chaperone shaping → dedup → contradiction check
  - write memories rows (status per §5.3), memory_sources rows
  - contradictions → treatments + review-queue rows (never overwrite)
```

Capture never blocks the agent and never loses data even if extraction fails — raw archival is step one and unconditional. Extraction can be re-run from the ledger at any time (it's a view).

### 4.4 Failure matrix

| Failure | Behavior | Trace |
|---|---|---|
| daemon down at recall | fail-open, visible marker | `recall_log.served=false` |
| daemon down at capture | hook writes transcript path to `~/memory/spool/`; daemon drains spool on start | spool file |
| extraction LLM error | raw already archived; extraction retried by micro-sleep | ledger `capture_ack` payload |
| Postgres down | daemon serves hot-set-only recall from RAM snapshot; capture spools | `recall_log.packet.degraded` |
| hook timeout (2s) | Claude Code proceeds; identical to fail-open | absent recall_log row for turn |

---

## 5. Capture pipeline detail

### 5.1 Extractor contract (the one LLM call per session)

Input: session's user/agent/tool events (non-meta), current active memories for the project (for dedup/contradiction), entity alias map.
Output: JSON list of candidates, each `{type, text, struct, entities, project|global, scope, sensitivity, authority_claim, confidence, activation, hedge_preserved: bool, source_event_ids[]}`.

Contract rules (enforced by validator, rejects on violation):
- **Hedge preservation is mandatory.** "Might switch to Qdrant" must extract as *considering*, never as *decided*. The validator rejects candidates whose text is more committal than the cited source events (checked by a cheap second pass on flagged modal verbs).
- Every candidate must cite ≥1 real `source_event_id` from this session.
- `directive` and `warning` types require explicit `struct` fields (condition/expiry, class/severity).
- Nothing may claim authority above `A1` and only direct user statements get `A1`.

### 5.2 Chaperone (deterministic shaping, not LLM)

Checks each candidate: source present, scope present, confidence present, sensitivity set, validity window set, activation conditions present for preferences, contradiction check run. Missing fields → candidate is held in review queue, not silently defaulted.

### 5.3 Promotion (quorum-lite, stingy per P6 — this generates the D4 evidence)

```
status assignment:
  active       ← authority A1 (direct explicit user statement this session)
                 AND type ∈ {directive, decision, constraint, commitment}
  active       ← any candidate the user explicitly confirms via review queue
  candidate    ← everything else well-formed        (retrievable ONLY into the
                                                     "candidate" packet lane, clearly labeled)
  priming      ← session-scoped context; expires end of session + 24h
  quarantined  ← authority Q, or failed chaperone, or extractor flagged untrusted origin
identity-tier writes (type=identity, global scope):
  NEVER auto-active. Requires: seen in ≥2 independent sessions
  AND user confirmation (Two-Person-Rule-lite).
```

D4 measurement built in: every packet logs which lane each item came from; S2 raters see whether `candidate`-lane items would have helped. If candidates keep proving useful, stinginess is costing us and the threshold loosens — with data, not taste.

### 5.4 Micro-sleep (nightly cron, the only consolidation in the slice)

- Retry failed extractions; drain spool.
- Contradiction sweep across the day's new memories vs. actives → treatments + review rows.
- Expire priming memories; apply half-life decay to `candidate` scores.
- Re-embed anything missing; run S12 quick-diff on a 20-query frozen set.
- Emit a one-page daily digest (markdown to `~/memory/digest/`) — the entire human UI of the slice, plus the review queue CLI.

---

## 6. Retrieval pipeline detail

### 6.1 Task classification (heuristic, no LLM)

Regex/keyword mapping to modes: `debug` (stack traces, file paths, error strings) · `decision` ("should we", "did we decide") · `state` ("where were we", "status", "continue") · `style` ("write", "email", "draft") · `general`. Mode sets channel weights and lane sizes. Misclassification cost is low because warnings and directives run unconditionally regardless of mode.

### 6.2 Channels

```
HOT      in-RAM: identity, global prefs, this project's project_state,
         open commitments/open_questions. Always included, pre-compiled.
FTS      websearch_to_tsquery over extracted exact terms
         (identifiers, paths, names) — precision channel.
VECTOR   pgvector cosine over prompt embedding, top 12,
         filtered to status IN (active, candidate) — recall channel.
WARNING  unconditional scan: active warnings + directives where
         project matches OR global, condition evaluates true,
         not expired. Never skipped, never budget-evicted.
```

### 6.3 Filters (rule-based court, P5)

Applied in order to every non-hot candidate:

1. **Validity:** `valid_to IS NULL OR valid_to >= today`; superseded → excluded, but its superseding memory gets a `supersedes_note` so the agent can say "you used to prefer X."
2. **Activation:** task mode ∉ `activation.exclude`; if `activation.task_type` set, mode must match — this is the rule that stops the concise-preference-vs-deep-research misfire.
3. **Directive precedence:** statute over common law — an applicable `directive` (A1) suppresses any conflicting `preference` (inferred) and the suppression is logged as a treatment.
4. **Scope/sensitivity:** `sealed` never enters a packet in the slice (escrow is post-slice); scope must match agent visa (slice visa: claude-code = work_private + project_shared + public).
5. **Canary check:** any `is_canary` row surviving to here → abort packet assembly for that item, alarm, ledger `veto` event. This is a hard S7 failure being caught live.
6. **Ambiguity logging:** any case where rules 1–4 felt insufficient (conflicting non-directive memories, stale-but-unconfirmed items) → logged with full context. This log becomes the labeled dataset that later decides D5 (rules vs. LLM judge).

### 6.4 Packet assembly (fixed lanes, 1,500-token budget)

```
Lane 1  DIRECTIVES & WARNINGS      reserved 300 tok, cannot be evicted
Lane 2  HOT (identity/prefs/state) reserved 350 tok
Lane 3  RETRIEVED ACTIVE            up to 600 tok, reranked
Lane 4  CANDIDATE (labeled)         up to 150 tok, clearly marked unconfirmed
Lane 5  OPEN LOOPS / COMMITMENTS    up to 100 tok
Rerank score (lane 3):
  0.35·semantic + 0.20·keyword + 0.15·recency
  + 0.15·useful_count_norm + 0.10·authority + 0.05·confirmation_recency
```

Packet rendering (what the model actually sees):

```markdown
## Memory (auto-recalled; cite mem_ ids when relying on these)
### Active directives & warnings
- ⚠ [directive, A1, mem_01J…] Do not modify the data backfill cron
  without asking — broke prod twice (evt_…, evt_…).
### About the user & this project
- [preference, confirmed 2026-06] Prefers short factual commit messages.
- [project_state, 2026-07-06] demo-app: /goal review feature in design;
  OpenRouter integration planned, not started.
### Possibly relevant (retrieved)
- [decision, 2026-05-14, supersedes earlier choice] exchange perps
  chosen as COT proxy — rationale: …
### Unconfirmed candidates (verify before relying)
- [candidate] the user may be considering Qdrant later (no decision made).
### Open loops
- Commitment: reply to the PM about task prioritization.
```

Every line carries type + date + implicit certainty (lane = certainty label — the slice's Oracle-Formatter-lite). IDs make the packet auditable and let the agent cite sources.

### 6.5 Veto (slice version)

Full pre-action veto interception is post-slice (needs PreToolUse hooks and a failure database with breadth). The slice ships veto as **Lane-1 blocking warnings**: `warning.class=failed_attempt` entries with evidence refs, injected unconditionally. S6 tests whether Lane-1 presence actually prevents repeat attempts; if the model ignores textual warnings, *that* is the evidence that hard PreToolUse veto interception must be built next.

---

## 7. Test harness & the evidence this slice must produce

Harness = `memoryd /admin/eval` + a frozen scenario repo:

- **S2 usefulness:** for each real task in the trial month, replay memory-off vs memory-on transcript openings; blind pairwise rating (you, weekly, ~15 min). Win/loss/draw logged.
- **S3 staleness:** seeded supersession fixtures (the Hermes pattern) + every real supersession that occurs during the trial becomes a permanent regression case.
- **S6 veto-as-warning:** seed 5 failed-attempt scenarios in a sandbox repo; measure repeat-attempt rate with/without Lane 1.
- **S7 scope/canary:** 10 canary memories (5 sealed-scope, 5 poisoned-looking) planted before day 1. Any surfacing = red alarm, immediate stop-and-fix.
- **S12 rebuild:** drop `mem_embeddings` + FTS, rebuild, diff packets on the frozen 50-query set. Run at week 2 and week 4.
- **Miss logging:** `miss_signals` populated by a trivial detector (user message matches "you forgot|I already told you|as I said") + manual flagging command. Not clever; just counted.

**What resolves the open decisions:**

| Decision | Evidence the slice produces |
|---|---|
| **D1** (zettel vs graph vs dual) | Which query modes fail on flat memory objects? If timeline/relationship questions (S3 variants, "why did we change X") degrade measurably, the graph earns its complexity. If not, dual-body objects may be enough far longer than expected. |
| **D2** (hooks vs MCP) | `recall_log.served` rate, fail-open frequency, latency p95, and whether the visible-marker turns correlate with worse outcomes. If hooks are ≥99% reliable at <700ms, D2 closes: hooks-primary, MCP additive. |
| **D4** (promotion stinginess) | Candidate-lane usefulness rate from S2 ratings vs. pollution incidents (bad actives needing demotion). The ratio *is* the threshold-tuning signal. |
| D5 (court judge) | The ambiguity log — size and character of cases rules couldn't cleanly settle. |
| D7 (consolidation budget) | Actual cost of one-extraction-per-session + nightly micro-sleep on a real month. |

---

## 8. Build order (dependency-driven, each step usable)

```
M1  Schema + archive + manifest + ledger writer.        (nothing reads yet)
M2  Capture path: hooks → spool/daemon → raw archival.  (evidence accrues from day 1,
                                                          even before recall exists)
M3  Extractor + chaperone + promotion + review CLI.     (memories accumulate)
M4  Recall path: hot set + FTS + warnings + packet.     (first useful injection;
                                                          no vectors yet — proves the
                                                          skeleton before the ML)
M5  Vector channel + rerank + full lane budgeting.
M6  Court rules + canaries + recall/miss logging.
M7  Eval harness + frozen scenarios + nightly micro-sleep.
M8  One-month live trial on real work. Weekly S2 ratings.
    → decision memo: D1/D2/D4 rulings + next-phase scope.
```

M2 before M4 is deliberate: capture-first means that by the time recall works, there is already real memory to recall — and raw evidence is being preserved from the very first day regardless of everything else.

---

## Appendix: what deliberately got harder to add later (and why that's fine)

- **No MCP server** → any second agent needs its own hook shim first. Acceptable: the slice has exactly one tenant by design.
- **No graph** → multi-hop relationship queries fall back to FTS+vector. That degradation is *the D1 measurement*, not a bug.
- **No LLM court** → nuanced applicability rulings are missed and logged. The log is the D5 dataset.
- **Rule-of-thumb rerank weights** → tuned only if S2 shows retrieval (not capture) is the bottleneck. Don't optimize the wrong stage.

---

## Appendix B: Open Knowledge Format (OKF) — assessment & post-slice plan

*Added 2026-07-08 after review of the OKF v0.1 draft spec (Google, github.com/GoogleCloudPlatform/knowledge-catalog). Status: recorded for the post-slice backlog; no change to slice scope.*

**What it is.** A minimal interchange format: directory of markdown files with YAML frontmatter; file path = concept ID; markdown links = untyped edges; `index.md` (progressive disclosure) and `log.md` (history) as reserved files; `type` is the only required field; consumption is mandated to be permissive (unknown types/fields/broken links must be tolerated).

**Ruling: export target, not substrate.** OKF lacks everything our governance depends on — status lifecycle, authority grades, bitemporal validity, supersession, scope/sensitivity, provenance threads. Its *permissive consumption* rule means our envelope fields in frontmatter would be silently ignored by other consumers, so **exporting to OKF strips governance by design**. Consequence: any OKF export must be pre-filtered at generation time (active status, exportable scopes only, sealed/private never written), not protected by frontmatter the consumer is free to ignore. Path-as-identity (fragile vs. ULIDs) and untyped links (vs. our typed temporal edges) confirm it cannot be canonical.

**Value to this project (post-slice backlog, in order):**

1. **B-OKF-1 — OKF export compiler** (~1 day against existing schema). Implements the deferred human-readable-vault milestone as a fourth dialect target: active memories → `memories/<type>/<id>.md` (frontmatter: `type`, `title`, `description`, `tags`, `timestamp` + `x_memoryd_*` extension fields incl. memory id, authority, valid_from, supersedes), generated `index.md` per directory, `log.md` from daily digests. Strictly a regenerated read-only projection — a view like the embeddings, never truth. Gives Obsidian/GitHub/graph-visualizer compatibility without inventing a vault layout.
2. **B-OKF-2 — OKF import with quarantine** (later). Third-party bundles ingest as `untrusted_external` scope, authority capped at C1; external `type: directive`/`warning` concepts import as inert reference only, never as actionable memory types. A poisoned bundle carrying standing instructions is precisely the memory-poisoning attack the firewall exists for.
3. **D1 evidence note.** OKF independently converged on our dual-body shape (markdown body + small queryable frontmatter). Record as supporting evidence for dual-body objects when the D1 decision memo is written after the trial.

**Out of scope for this project but flagged:** OKF's center of gravity is data-catalog knowledge (schemas, metrics, join paths) — a strong fit for enterprise data-catalog documentation as a separate initiative.
