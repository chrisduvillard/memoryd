# Prompt for a supervised Hermes installation

Paste the prompt below into Hermes and attach or link the repository. Hermes
will guide the rollout, but you will run every command block in a separate
normal Linux terminal. This prevents an active Hermes process from changing or
restarting its own memory provider.

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
  expose secret values in chat, shell history, logs, documentation, or backups.
  The only allowed persistence is the owner-private ~/memory/config.json that
  memoryd install writes for its systemd user service. Tell me when the runbook
  requires interactive secret entry in my terminal.
- Do not execute any rollout command with your own shell or tool calls. Present
  one normal-shell command block at a time for me to run in a separate normal
  Linux terminal, wait for my pasted output, validate it, and only then continue.
- Never activate or restart your own memory provider from this active chat.
  At the activation gate, give me the exact runbook block, tell me to exit all
  Hermes chat/TUI sessions cleanly, and stop. I will run the activation block
  and all four checks in a normal terminal, restore any previously active
  gateway, and start a new Hermes chat/TUI only after every check passes.
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
6. Stop at the activation boundary. Give me the exact activation block and this
   resume sentence before I exit every active Hermes chat/TUI:
   "Resume the memoryd rollout after activation. I ran hermes memory status,
   hermes memoryd config, memoryd status, and hermes memoryd status in a normal
   terminal; all four checks passed, and any previously active gateway is
   healthy. Validate the recorded output before continuing."
7. Require me to run the activation block and all four checks in that normal
   terminal, then restore any previously active gateway. Only after they pass
   may I start a new Hermes chat/TUI and use the resume sentence. In the new
   session, validate the recorded output, exact URL, zero dead letters, no
   durability fault, drained queue, and healthy restored gateway.
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

The installation is connected only after the operator exits every active
Hermes chat/TUI and a normal terminal reports healthy output from `hermes
memory status`, `hermes memoryd config`, `memoryd status`, and `hermes memoryd
status`, with the exact localhost URL, zero dead letters, no durability fault,
a drained queue, and any previously active gateway restored. Start a new
Hermes chat/TUI only after those checks pass. The installation becomes
production-ready only after the complete canary passes.
