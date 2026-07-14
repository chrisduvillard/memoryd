# Production Linux rollout

This runbook is the audit, troubleshooting, and recovery reference for one
authoritative memoryd 0.3.1 instance beside Hermes on a Linux laptop. The
guided installer is the supported production path. Manual commands in this
document are for inspection, an additional backup or restore drill, and
emergency rollback—not a second installation recipe.

The rollout never reads, repairs, deletes, or migrates a Windows installation
or its data. Run every command from a normal Linux terminal, never from a
Hermes chat, TUI, gateway tool call, cron job, or subagent.

## Fixed release inputs

- memoryd and bundled plugin: `v0.3.1`
- Hermes Agent package: exactly `hermes-agent==0.16.0`
- Hermes source tag: `v2026.6.5`
- Hermes resolved commit:
  `3c231eb3979ab9c57d5cd6d02f1d577a3b718b43`
- daemon and plugin URL: `http://127.0.0.1:7437`
- managed memory home: `~/memory`
- database: Docker PostgreSQL 16 with pgvector, bound to localhost
- extraction: OpenRouter; embeddings: Voyage

Do not substitute upstream Hermes `main`. The exact-version validator runs
through the interpreter behind the installed `hermes` executable and compares
the installed contract with the copy bundled in the memoryd wheel.

## 1. Prerequisites and operator boundary

The laptop needs:

- Linux with a working systemd user manager
- Docker with permission to start a localhost container
- `git` (required by the immutable `pipx` Git URL)
- `pipx` and Python 3.13 (Python 3.11 is supported if 3.13 is unavailable)
- Hermes Agent already installed and available as `hermes`
- OpenRouter and Voyage API keys
- an interactive terminal; stdin and stdout must both be TTYs

Finish any in-flight response and exit every Hermes chat/TUI before starting.
Hermes must not install or activate its own memory provider. The installer may
stop a running Hermes gateway while it switches providers and will restore the
gateway to its prior state.

The authoritative profile comes from `$HERMES_HOME` and Hermes's
`active_profile` marker. An explicit `.../profiles/<name>` path wins. Otherwise
an absent marker or literal `default` selects the root; a valid named marker
selects `$HERMES_HOME/profiles/<name>`. The authoritative root itself must be
owned by the effective user and mode `0700`; if it is too broad, use the exact
shell-quoted `chmod 700 -- <resolved-root>` command in the error and retry. The
resolved root can differ from `$HERMES_HOME` when it names a profile. Inside
that private root, the installer
accepts the owned, non-group/other-writable `0755` profile directories and
`0644` `active_profile` marker created by Hermes 0.16.0. Symlinks, special
files, wrong owners, writable descendants, invalid or multiline names,
traversal, ambiguity, and missing selected profiles are refused. The installer
does not invent a missing selected profile.

## 2. Guided installation

Run exactly:

```bash
pipx install --python python3.13 \
  'git+https://github.com/chrisduvillard/memoryd.git@v0.3.1'
memoryd install --hermes
```

The command performs all non-mutating checks first:

1. Linux, TTY, systemd user-manager, exact Hermes version and packaged contract
2. authoritative profile resolution and owner-only permission checks
3. fresh or recognized managed `~/memory` classification
4. explicit operator confirmation
5. hidden credential collection and minimal live provider probes

Only after those checks succeed does it create or adopt managed state. It
installs PostgreSQL 16/pgvector with a random password, applies migrations
001–007, writes owner-only memoryd configuration, copies the wheel-bundled
plugin, installs the daemon and backup timer, starts the daemon, and creates
and verifies one initial snapshot.

### Unsupported Hermes version

A version or contract mismatch stops without changing memoryd or Hermes. Use
the exact command printed by the installer, which pins the resolved commit:

```bash
pipx install --force --python python3.13 \
  'git+https://github.com/NousResearch/hermes-agent.git@3c231eb3979ab9c57d5cd6d02f1d577a3b718b43'
```

Then close Hermes again and rerun `memoryd install --hermes`. The installer
never repairs or upgrades Hermes automatically.

### Secret handling

Do not pass secrets as command-line arguments and do not paste them into chat.
Missing `OPENROUTER_API_KEY` and `VOYAGE_API_KEY` values are collected with
hidden `getpass` prompts. Both must pass minimal live OpenRouter completion and
Voyage embedding probes before target mutation. Provider failures are reported
without keys or response bodies.

The accepted values are stored only in the `0600` memoryd configuration under
the `0700` memory home, together with `MEMORYD_LLM=openrouter` and
`MEMORYD_EMBED=voyage`. Snapshot manifests record secret names that must be
supplied after restore, never their values. Configuration and local backups are
owner-only but not encrypted at rest.

### Reruns and refusal

The command is idempotent for a positively recognized managed memoryd
installation. It revalidates credentials and all boundaries, recopies the
identical wheel plugin, reapplies only missing migrations, verifies a new
initial snapshot, and rechecks activation.

An unknown or partially shaped nonempty `~/memory` is refused without
modification. Investigate it as evidence; do not rename, delete, merge, or
adopt it by hand. Restore also refuses a nonempty home or target database and
never performs destructive in-place recovery.

## 3. Transactional activation and success report

Immediately before activation the installer records the current provider and
whether the Hermes gateway is running. It stops only a running gateway, sets
the external provider, verifies the selected profile and localhost URL, then
runs these four checks in order:

```bash
hermes memory status
hermes memoryd config
memoryd status
hermes memoryd status
```

Success requires the exact URL, no durability fault, zero dead letters, and an
incoming/processing queue that drains to zero. A previously running gateway
must be running again before the command returns success. The completion report
names the authoritative profile, daemon URL, verified initial snapshot, health
checks, restored gateway state, and canary next step; it never prints secrets.

On failure, SIGINT, or SIGTERM, the transaction restores the previous provider
(or built-in-only state) and the previous gateway state. It retains the
memoryd database, spool, archive, configuration, backups, and logs. All
dead-letter evidence is preserved. If rollback is incomplete, the command
reports the failed restoration stage and remains nonzero. Do not open a new Hermes session
until the provider and gateway state are known.

## 4. Post-install audit

The guided command already runs the four status checks. Operators may repeat
these read-only checks after installation:

```bash
systemctl --user is-active memoryd.service
systemctl --user is-enabled memoryd-backup.timer
systemctl --user list-timers memoryd-backup.timer memoryd-microsleep.timer
docker inspect memoryd-pgvector --format '{{json .HostConfig.PortBindings}}'
memoryd status
memoryd doctor
hermes memory status
hermes memoryd config
hermes memoryd status
```

PostgreSQL must be bound to `127.0.0.1`; the daemon is also localhost-only.
This design intentionally has no TLS or network authentication. The selected
profile's `$HERMES_HOME/plugins/memoryd/plugin.yaml` and `memoryd.json` must be
owner-only and the latter must contain only the localhost URL.

Daily snapshots run at 02:35. The user service stops memoryd, creates and
verifies the snapshot, and restarts memoryd through `ExecStopPost` even when
creation fails. It retains 14 daily snapshots. Check failures with:

```bash
journalctl --user -u memoryd-backup.service
memoryd backup list
memoryd backup verify ~/memory/backups/<UTC>-v1
```

## 5. Additional backup and disposable restore drill

This is the only routine manual mutation documented here. Stop the daemon for
an additional coherent snapshot and always restart it:

```bash
set -euo pipefail
systemctl --user stop memoryd.service
trap 'systemctl --user start memoryd.service' EXIT
SNAPSHOT="$(memoryd backup create --retain 14 | sed -n 's/^created //p')"
test -n "$SNAPSHOT"
memoryd backup verify "$SNAPSHOT"
systemctl --user start memoryd.service
trap - EXIT
memoryd status
```

Restore only into a new empty database and a new target home while every target
daemon is stopped:

```bash
createdb memoryd_restore_drill
memoryd backup restore "$SNAPSHOT" \
  --dsn 'postgresql://restore-user@localhost/memoryd_restore_drill' \
  --home ~/memory-restore-drill
MEMORYD_HOME=~/memory-restore-drill memoryd doctor
```

Never restore over `~/memory` or the live database. Re-enter required secrets
after the drill; do not copy them from the live config. Preserve the snapshot,
verification output, restored doctor output, and database name as canary
evidence.

## 6. Emergency rollback without evidence loss

The guided installer rolls back activation failures automatically. This manual
procedure is only for a later canary or operational failure. Exit every Hermes
chat/TUI and work from a normal terminal.

1. Record `hermes memory status`, `hermes memoryd status`, and `memoryd status`.
2. Stop a running Hermes gateway and record that it was running.
3. Run `hermes memory off` and verify `hermes memory status` reports no external
   provider.
4. Restart the gateway only if it was running before rollback.
5. Stop `memoryd.service` after Hermes is confirmed built-in-only.

Do not uninstall memoryd, delete the database container or volume, remove queue
jobs, edit dead letters, prune archives, restore over the live instance, or
reuse request IDs. Preserve at least:

- `$HERMES_HOME/spool/memoryd/`, including incoming, processing, dead-letter,
  identity reservations, and fault state
- `~/memory/spool/`, `~/memory/archive/`, `~/memory/backups/`,
  `~/memory/config.json`, and managed credential metadata
- the `memoryd-pgvector` container metadata and persistent volume
- `journalctl --user -u memoryd.service` and backup-service logs
- all status outputs and the last successful snapshot verification

Copy evidence only to owner-readable storage and keep the database and archive
paired. Diagnose against a cloned database/home. After repair, restart the
canary from day zero.

## 7. Promotion gate

Installation success does not equal production promotion. Follow
[CANARY_SCORECARD.md](CANARY_SCORECARD.md) for at least 14 complete days and 200
real Hermes turns. Promotion still requires zero unexplained loss, duplicate
batches, dead letters, integrity defects, or visa leakage; ten planted
out-of-visa canaries; clean daily doctor/status evidence; recall p95 below
700 ms; fail-open below 1%; at least 18 of 20 expected replay memories; no
sealed or superseded memory served; resolvable extraction citations; no
hedge-to-commitment overstatement; and a verified full disposable restore.
