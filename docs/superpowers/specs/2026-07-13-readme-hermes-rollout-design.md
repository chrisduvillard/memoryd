# README and Hermes rollout prompt design

**Status:** Approved direction on 2026-07-13

## Problem

The README has a clear cross-platform quickstart, but it presents an unpinned
installation from the default branch as the main path. The production Linux
and Hermes runbook is safer and more exact, yet readers discover it near the
end of the page. A user who asks an active Hermes session to install its own
memory provider may also let that session modify or restart the runtime that is
executing the rollout.

## Goals

- Keep the short cross-platform quickstart for evaluation and local use.
- Give production Linux and Hermes users an obvious, release-pinned path.
- Supply one reusable prompt that lets Hermes supervise a safe installation.
- Keep API-key values out of prompts, shell history, logs, and backups.
- Preserve Windows installations and data; the Linux rollout starts clean.
- Keep the detailed production runbook and canary scorecard authoritative.

## Approaches considered

### 1. Replace the quickstart with the full production procedure

This makes the safest path dominant, but turns the README into a long operator
manual and makes evaluation look harder than it is.

### 2. Keep only the existing quickstart and link the runbook more prominently

This keeps the README compact, but it leaves readers to translate a long
runbook into an agent-safe workflow and does not address self-modification from
an active Hermes chat.

### 3. Keep both paths and add a supervised Hermes prompt

This is the selected approach. The README will distinguish evaluation from
production, pin production commands to v0.3.0, summarize the safety gates, and
link to a canonical copy-paste prompt. The detailed runbook will remain the
source of truth.

## Documentation design

### README

The README will:

1. Add a production-Hermes link to the top navigation.
2. Add a short path-selection table before installation.
3. Keep the general quickstart, but label it for evaluation and pin its Git
   install to `v0.3.0`.
4. Add a prominent Linux/Hermes production section that states the fixed
   versions, clean-home requirement, localhost-only topology, secret-handling
   rules, activation boundary, verification commands, backup drill, and
   14-day/200-turn canary.
5. Link the production runbook, canary scorecard, release, and reusable Hermes
   prompt near the commands they govern.
6. Replace stale status language with exact current test evidence and a candid
   statement that production promotion still depends on the canary.

The README will summarize rather than duplicate the full runbook. This reduces
the chance that two long procedures drift apart.

### Canonical Hermes prompt

Add `docs/HERMES_INSTALL_PROMPT.md` and reproduce its prompt in the user
handoff. The prompt will tell Hermes to:

- use `https://github.com/chrisduvillard/memoryd` at tag `v0.3.0`;
- treat `docs/PRODUCTION_ROLLOUT.md` and `docs/CANARY_SCORECARD.md` as
  authoritative;
- verify Hermes Agent 0.16.0 and its resolved source commit;
- preserve Windows data and stop if Linux `~/memory` already exists;
- never request, echo, log, or persist secret values in chat;
- avoid running rollout commands inside a Hermes tool call;
- present one normal-shell block at a time and validate its output;
- pause before the operator exits active Hermes sessions and activates the
  provider;
- verify `memoryd status`, `hermes memory status`, and
  `hermes memoryd status` after restart;
- run integration and restore drills only in disposable homes and databases;
- create and verify the first production snapshot;
- begin the canary and use evidence-preserving rollback if a gate fails.

The prompt will require concise progress reports with four states: completed,
waiting for operator action, blocked, and failed with preserved evidence.

## Safety boundaries

- The prompt cannot contain OpenRouter or Voyage key values. The operator
  enters them interactively in a normal terminal.
- The active Hermes session may inspect and explain the runbook, but it must
  not replace, activate, or restart its own provider.
- Production uses exactly `~/memory`; disposable drills use separate temporary
  homes and databases.
- Existing homes, databases, spools, archives, backups, containers, and volumes
  are evidence. The procedure stops instead of deleting or overwriting them.
- README shortcuts never weaken a runbook gate.

## Verification

- Check every relative Markdown link and every referenced repository path.
- Compare all pinned versions, URLs, paths, ports, and commands with the
  production runbook.
- Scan the documentation for secret-shaped example values and unpinned install
  commands.
- Render or lint Markdown when local tooling permits.
- Run the full Python regression suite to confirm a documentation-only diff
  does not disturb packaging or tests.
- Review the final diff for duplication, contradictory instructions, and
  accidental changes outside the two documentation files and this design.

## Non-goals

- Automating the 14-day canary or claiming production promotion before it
  completes.
- Migrating or repairing Windows data.
- Changing memoryd, the Hermes plugin, the release tag, or runtime behavior.
- Replacing the detailed production runbook with README prose.
