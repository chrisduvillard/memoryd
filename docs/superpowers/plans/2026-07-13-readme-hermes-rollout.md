# README and Hermes Rollout Prompt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the safe v0.3.0 Linux/Hermes production path obvious without losing the short cross-platform quickstart, and publish a copy-paste prompt that keeps provider activation outside the active Hermes session.

**Architecture:** Keep `docs/PRODUCTION_ROLLOUT.md` and `docs/CANARY_SCORECARD.md` authoritative. Add one focused prompt document that turns those runbooks into a supervised operator dialogue, then make README route readers to either the evaluation quickstart or the production path without duplicating the full runbook.

**Tech Stack:** CommonMark/GitHub-flavored Markdown, Bash command examples, GitHub relative links, Python pytest regression suite.

## Global Constraints

- Production memoryd and bundled Hermes plugin remain exactly `v0.3.0`.
- Production Hermes remains `hermes-agent==0.16.0`, source tag `v2026.6.5`, resolved commit `3c231eb3979ab9c57d5cd6d02f1d577a3b718b43`.
- Production uses exactly `~/memory`, Docker PostgreSQL 16 with pgvector on `127.0.0.1`, and memoryd at `http://127.0.0.1:7437`.
- OpenRouter and Voyage secret values never appear in chat, shell history, logs, documentation, or backups.
- An active Hermes session may supervise but must not execute, activate, or restart its own memory-provider rollout.
- Existing Linux homes, databases, spools, archives, backups, containers, and volumes are preserved as evidence; stop instead of deleting or overwriting them.
- Windows data is never read, repaired, deleted, or migrated.
- Production promotion still requires the complete 14-day, minimum-200-turn canary.

---

### Task 1: Publish the canonical supervised Hermes prompt

**Files:**
- Create: `docs/HERMES_INSTALL_PROMPT.md`
- Reference: `docs/PRODUCTION_ROLLOUT.md`
- Reference: `docs/CANARY_SCORECARD.md`

**Interfaces:**
- Consumes: the fixed release inputs, gates, drill, snapshot, and rollback procedure in the two authoritative runbooks.
- Produces: a stable `docs/HERMES_INSTALL_PROMPT.md` target that README and the user handoff can link and copy verbatim.

- [ ] **Step 1: Confirm every prompt invariant against the runbooks**

Run:

```powershell
rg -n "v0\.3\.0|0\.16\.0|3c231e|127\.0\.0\.1:7437|~/memory|normal user shell|14-day|200-turn|Rollback" docs/PRODUCTION_ROLLOUT.md docs/CANARY_SCORECARD.md
```

Expected: each fixed version, endpoint, home, shell boundary, promotion duration, turn count, and rollback section appears in the authoritative documentation.

- [ ] **Step 2: Create the complete supervised-install prompt**

Create `docs/HERMES_INSTALL_PROMPT.md` with this structure and wording:

````markdown
# Prompt for a supervised Hermes installation

Paste the prompt below into Hermes and attach or link the repository. Hermes
will guide the rollout, but you will run every state-changing command in a
separate Linux terminal. This prevents an active Hermes process from replacing
or restarting its own memory provider.

Do not paste API keys into the Hermes chat. Enter them only at the runbook's
interactive terminal prompts.

## Copy-paste prompt

```text
You are supervising a production installation of memoryd beside this Hermes
Agent on my Linux laptop.

Repository: https://github.com/chrisduvillard/memoryd
memoryd release: v0.3.0

Objective:
Install a new authoritative memoryd instance, connect this Hermes profile to
it, verify backup and recovery, and begin the production canary. The Windows
installation and all Windows data are out of scope: do not inspect, repair,
delete, or migrate them.

Authoritative instructions:
1. Read the repository's docs/PRODUCTION_ROLLOUT.md in full.
2. Read docs/CANARY_SCORECARD.md in full.
3. Follow those files exactly. If this prompt and a runbook differ, stop and
   follow the runbook.

Non-negotiable constraints:
- Pin memoryd and its bundled plugin to v0.3.0.
- Pin Hermes Agent to package 0.16.0, source tag v2026.6.5, resolved commit
  3c231eb3979ab9c57d5cd6d02f1d577a3b718b43.
- Use exactly ~/memory for production, Docker PostgreSQL 16 plus pgvector bound
  to 127.0.0.1, and http://127.0.0.1:7437 for memoryd.
- Configure MEMORYD_LLM=openrouter and MEMORYD_EMBED=voyage.
- Never ask me to paste an OpenRouter key or Voyage key into this chat. Never
  echo, log, persist, or place secret values in shell history. Tell me when the
  runbook requires interactive secret entry in my terminal.
- Do not execute rollout commands with your own shell or tool calls. Present
  one normal-shell command block at a time for me to run in a separate terminal,
  wait for my pasted output, validate it, and only then continue.
- Never activate or restart your own memory provider from this active chat.
  At the activation gate, give me the exact runbook block, tell me to exit all
  Hermes chat/TUI sessions cleanly, and wait. I will run the block in a normal
  terminal and start a new Hermes session for post-activation verification.
- Never delete or overwrite an existing home, database, spool, archive, backup,
  Docker container, or volume. If ~/memory already exists, stop and report it.
- Run integration and restore tests only against disposable homes, ports,
  containers, and databases from the runbook.
- Do not claim production readiness until the entire 14-day, minimum-200-turn
  canary passes every scorecard gate.

Workflow:
1. Start with the runbook preflight and authoritative HERMES_HOME selection.
2. Verify or remediate the exact Hermes version and commit.
3. Clone memoryd tag v0.3.0 and require the contract checker to print COMPATIBLE.
4. Install memoryd in its own pipx environment and pass the installed-Hermes
   lifecycle validator.
5. Guide interactive OpenRouter and Voyage secret entry, run memoryd install,
   and verify permissions, systemd user services, timers, plugin location,
   localhost bindings, memoryd status, and memoryd doctor.
6. Stop at the activation boundary. Before I exit, give me this resume sentence:
   "Resume the memoryd rollout at post-activation verification. Validate all
   status output before continuing."
7. In the restarted Hermes session, verify hermes memory status, hermes memoryd
   config, memoryd status, and hermes memoryd status. Require the exact URL,
   zero dead letters, no durability fault, and a drained queue.
8. Guide the disposable integration and restore drill without touching
   production data.
9. Create and verify the first production snapshot while guaranteeing daemon
   restart after success or failure.
10. Begin the canary using docs/CANARY_SCORECARD.md. If any gate fails, use the
    runbook's evidence-preserving rollback and restart the canary from day zero
    only after repair.

Stop immediately and preserve evidence if a command fails, a version drifts,
the contract checker is not COMPATIBLE, a localhost binding is wrong, a status
is unhealthy, a queue has unexplained jobs, a dead letter or durability fault
appears, backup verification fails, or a target is nonempty.

For every reply, use this compact format:
State: completed | waiting for operator action | blocked | failed
Completed: checks that passed, with decisive output
Current gate: the runbook section and gate being evaluated
Next operator action: exactly one normal-shell block, or the required manual
action
Stop reason: none, or the exact failure and preserved evidence paths

Begin by summarizing the fixed release inputs and giving me only the preflight
block from section 1 of docs/PRODUCTION_ROLLOUT.md. Do not skip ahead.
```

## What success looks like

The installation is connected when a restarted Hermes session reports healthy
output from both status commands, with the exact localhost URL, zero dead
letters, no durability fault, and a drained queue. It becomes production-ready
only after the complete canary passes.
````

- [ ] **Step 3: Check the prompt for forbidden behavior and missing gates**

Run:

```powershell
rg -n "Do not execute|Never activate|Never ask me|Never delete|14-day|200-turn|COMPATIBLE|backup|restore|rollback|dead letters|durability fault" docs/HERMES_INSTALL_PROMPT.md
rg -n "sk-[A-Za-z0-9]|pa-[A-Za-z0-9]|OPENROUTER_API_KEY=|VOYAGE_API_KEY=" docs/HERMES_INSTALL_PROMPT.md
```

Expected: the first command finds all safety gates; the second command prints nothing.

- [ ] **Step 4: Commit the canonical prompt**

```powershell
git add docs/HERMES_INSTALL_PROMPT.md
git commit -m "docs: add supervised Hermes install prompt"
```

Expected: one documentation commit containing only the new prompt.

---

### Task 2: Route README readers to evaluation or production

**Files:**
- Modify: `README.md:13`
- Modify: `README.md:69-126`
- Modify: `README.md:247-259`
- Reference: `docs/HERMES_INSTALL_PROMPT.md`
- Reference: `docs/PRODUCTION_ROLLOUT.md`
- Reference: `docs/CANARY_SCORECARD.md`

**Interfaces:**
- Consumes: the canonical prompt from Task 1 and the existing production runbooks.
- Produces: a README with an evaluation quickstart, a prominent production-Hermes route, pinned installation examples, and an evidence-based release status.

- [ ] **Step 1: Update top navigation and install-path selection**

Replace the centered navigation with:

```markdown
[**Quickstart**](#-choose-your-install-path) · [**Production Hermes**](#-production-hermes-on-linux) · [**Daily use**](#-daily-use) · [**Docs**](docs/REFERENCE.md) · [**Architecture**](docs/ARCHITECTURE.md)
```

Rename `## ⚡ Install (2 minutes)` to `## ⚡ Choose your install path`, then add:

```markdown
| Goal | Use this path |
|---|---|
| Evaluate memoryd or connect Claude Code on Windows, macOS, or Linux | Follow the quickstart below. |
| Make memoryd the production provider for Hermes on Linux | Use the [supervised Hermes prompt](docs/HERMES_INSTALL_PROMPT.md) and [production runbook](docs/PRODUCTION_ROLLOUT.md). |

### Quickstart (evaluation and local use)
```

Keep the existing requirements paragraph below that heading.

- [ ] **Step 2: Pin the evaluation install and remove secret-shaped examples**

Replace the quickstart block with:

```bash
# Optional: set OPENROUTER_API_KEY in your shell to enable fact extraction.
python -m pip install 'git+https://github.com/chrisduvillard/memoryd.git@v0.3.0'
memoryd install
memoryd status                     # Everything green? The local install is ready.
```

Expected: README contains no unpinned `pip install git+https://github.com/chrisduvillard/memoryd` command and no API-key-shaped example value.

- [ ] **Step 3: Add the production Linux/Hermes section**

Insert this section after `### 🤝 Connect Hermes Agent` and before daily use:

````markdown
## 🚀 Production Hermes on Linux

Use the hardened path when Hermes will depend on memoryd for real work. It pins
[memoryd v0.3.0](https://github.com/chrisduvillard/memoryd/releases/tag/v0.3.0)
and Hermes Agent 0.16.0, creates a clean authoritative `~/memory`, keeps
PostgreSQL and the daemon on localhost, verifies backup and restore, and then
starts a 14-day/200-turn canary.

> **Do not install or activate memoryd from an active Hermes tool call.** Give
> Hermes the [supervised installation prompt](docs/HERMES_INSTALL_PROMPT.md),
> run each state-changing block yourself in a separate Linux terminal, and exit
> every active Hermes chat/TUI before the activation gate.

The [production runbook](docs/PRODUCTION_ROLLOUT.md) is authoritative. It stops
on an existing `~/memory` or nonempty restore target and never migrates the
Windows installation. Enter OpenRouter and Voyage keys only at its interactive
terminal prompts; do not paste them into chat.

After activation and a clean Hermes restart, require all four checks:

```bash
hermes memory status
hermes memoryd config
memoryd status
hermes memoryd status
```

The Hermes report must show `http://127.0.0.1:7437`, zero dead letters, no
durability fault, and a queue that drains to zero. Complete the disposable
integration/restore drill and verify the first production snapshot before
starting the [canary scorecard](docs/CANARY_SCORECARD.md). Treat the instance as
a production candidate until every canary gate passes.
````

- [ ] **Step 4: Improve Learn more and Status**

Add this item to `## 📚 Learn more` before the production runbook:

```markdown
- [docs/HERMES_INSTALL_PROMPT.md](docs/HERMES_INSTALL_PROMPT.md) — copy-paste prompt for a supervised, self-modification-safe Hermes installation
```

Replace the Status paragraph with:

```markdown
v0.3.0 is a production candidate. Its release passed 265 local regression
tests with 13 environment-dependent skips and 22 subtests, plus the GitHub
Python 3.11/3.13 matrix. The hardened path adds durable Hermes queuing,
idempotent writes, owner-private credentials, verified backups, and safe
restore refusal. Production promotion still requires the documented 14-day,
minimum-200-turn canary; until that passes, keep the rollback path and all
evidence artifacts available.
```

- [ ] **Step 5: Validate links, pins, secrets, and Markdown whitespace**

Run:

```powershell
git diff --check
rg -n "HERMES_INSTALL_PROMPT|PRODUCTION_ROLLOUT|CANARY_SCORECARD|v0\.3\.0|0\.16\.0|127\.0\.0\.1:7437|14-day|200-turn" README.md docs/HERMES_INSTALL_PROMPT.md
rg -n "pip install git\+https://github\.com/chrisduvillard/memoryd(?:\.git)?$|sk-[A-Za-z0-9]|pa-[A-Za-z0-9]|OPENROUTER_API_KEY=|VOYAGE_API_KEY=" README.md docs/HERMES_INSTALL_PROMPT.md
```

Expected: `git diff --check` succeeds; the first search finds every pin and link; the second search prints nothing.

- [ ] **Step 6: Verify every relative Markdown link target**

Run:

```powershell
python -c "import pathlib,re,sys; files=[pathlib.Path('README.md'),pathlib.Path('docs/HERMES_INSTALL_PROMPT.md')]; bad=[]; [(bad.append((str(f),t)) if not (f.parent/t.split('#',1)[0]).resolve().exists() else None) for f in files for t in re.findall(r'\[[^]]+\]\((?!https?://|#)([^)]+)\)',f.read_text(encoding='utf-8')) if t.split('#',1)[0]]; print(*[f'{f}: {t}' for f,t in bad],sep='\n'); sys.exit(bool(bad))"
```

Expected: exit 0 with no output.

- [ ] **Step 7: Run the full regression suite**

Run:

```powershell
python -m pytest -q
```

Expected on the current Windows host: `265 passed, 13 skipped, 22 subtests passed`.

- [ ] **Step 8: Commit the README improvement**

```powershell
git add README.md
git commit -m "docs: clarify production Hermes rollout"
```

Expected: one documentation commit containing only README changes.

---

### Task 3: Final documentation consistency review

**Files:**
- Verify: `README.md`
- Verify: `docs/HERMES_INSTALL_PROMPT.md`
- Verify: `docs/PRODUCTION_ROLLOUT.md`
- Verify: `docs/CANARY_SCORECARD.md`
- Verify: `docs/superpowers/specs/2026-07-13-readme-hermes-rollout-design.md`

**Interfaces:**
- Consumes: Task 1 and Task 2 documentation.
- Produces: review evidence that the short paths do not weaken or contradict the authoritative runbooks.

- [ ] **Step 1: Review the complete branch diff**

Run:

```powershell
git diff origin/main...HEAD -- README.md docs/HERMES_INSTALL_PROMPT.md docs/superpowers/specs/2026-07-13-readme-hermes-rollout-design.md docs/superpowers/plans/2026-07-13-readme-hermes-rollout.md
```

Expected: only the approved documentation files change; no runtime, test, migration, workflow, or release file changes.

- [ ] **Step 2: Compare the shortened guidance with authoritative gates**

Check each shortened README or prompt claim against the exact matching passage in `docs/PRODUCTION_ROLLOUT.md` and `docs/CANARY_SCORECARD.md`. Reject the change for any version, path, port, command, permission, activation, backup, restore, rollback, duration, or count mismatch.

- [ ] **Step 3: Verify clean worktree and commit inventory**

Run:

```powershell
git status --short --branch
git log --oneline origin/main..HEAD
```

Expected: clean worktree and four documentation commits: design, implementation
plan, canonical prompt, and README improvement.
