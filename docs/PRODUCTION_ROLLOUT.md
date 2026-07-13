# Production Linux rollout

This runbook installs one clean, authoritative memoryd 0.3.0 instance beside
Hermes on the Linux laptop. It does not read, repair, delete, or migrate the
Windows installation or its data. Run every command below on Linux.

## Fixed release inputs

- memoryd and the bundled Hermes plugin: `v0.3.0`
- Hermes Agent package: `hermes-agent==0.16.0`
- Hermes source tag: `v2026.6.5`
- Hermes resolved commit:
  `3c231eb3979ab9c57d5cd6d02f1d577a3b718b43`
- memoryd API: `http://127.0.0.1:7437`
- database: Docker PostgreSQL 16 with pgvector, bound to `127.0.0.1`

The [official Hermes release](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.6.5)
names package version 0.16.0 and source tag `v2026.6.5`. Do not substitute
upstream `main` during this rollout.

## 1. Preflight and pin Hermes

Install Docker, Python 3.11 or 3.13, `pipx`, `git`, and PostgreSQL client tools.
Keep the existing Hermes installation only if its package and runtime contract
match the pin. Set `HERMES_PY` to the Python executable inside the environment
that provides the `hermes` command.

```bash
set -euo pipefail
docker version
python3 --version
pipx --version
hermes --version

export HERMES_PY=/absolute/path/to/hermes-environment/bin/python
"$HERMES_PY" - <<'PY'
from importlib.metadata import version
actual = version("hermes-agent")
assert actual == "0.16.0", f"expected hermes-agent 0.16.0, got {actual}"
print(f"hermes-agent {actual}")
PY
```

Check out memoryd's release source for the immutable contract checker and the
disposable verification suite:

```bash
git clone --branch v0.3.0 --depth 1 \
  https://github.com/chrisduvillard/memoryd.git /tmp/memoryd-v0.3.0
cd /tmp/memoryd-v0.3.0
"$HERMES_PY" scripts/check_hermes_contract.py
```

The checker must print `COMPATIBLE`. If Hermes came from a source checkout,
also require this exact result:

```bash
test "$(git -C /path/to/hermes-agent rev-parse HEAD)" = \
  3c231eb3979ab9c57d5cd6d02f1d577a3b718b43
```

## 2. Install memoryd in its own pipx environment

```bash
pipx install --python python3.13 \
  "git+https://github.com/chrisduvillard/memoryd.git@v0.3.0"
memoryd --help
python3 - <<'PY'
from pathlib import Path
assert not Path.home().joinpath("memory", "config.json").exists(), \
    "clean rollout requires an unused Linux memory home"
PY
```

Use Python 3.11 in the `pipx install` command if Python 3.13 is unavailable.
Do not continue over a pre-existing Linux memory home; move to a new Linux
account or choose an unused `MEMORYD_HOME` and investigate the old home
separately.

## 3. Supply provider secrets and install

Read secrets interactively so they never enter shell history. `memoryd
install` stores them in the owner-readable Linux configuration used by the
systemd user service. The backup manifest records their names, never values.

```bash
read -rsp 'OpenRouter API key: ' OPENROUTER_API_KEY; echo
read -rsp 'Voyage API key: ' VOYAGE_API_KEY; echo
export OPENROUTER_API_KEY VOYAGE_API_KEY
export MEMORYD_LLM=openrouter
export MEMORYD_EMBED=voyage

memoryd install
unset OPENROUTER_API_KEY VOYAGE_API_KEY

chmod 700 "$HOME/memory"
test "$(stat -c '%a' "$HOME/memory/config.json")" = 600
systemctl --user is-active memoryd.service
systemctl --user is-enabled memoryd-backup.timer
systemctl --user list-timers memoryd-backup.timer memoryd-microsleep.timer
```

A fresh install creates `memoryd-pgvector` with a random PostgreSQL password,
a persistent Docker volume, and a localhost-only published port. Confirm the
binding instead of assuming it:

```bash
docker inspect memoryd-pgvector \
  --format '{{json .HostConfig.PortBindings}}'
memoryd status
memoryd doctor
```

The PostgreSQL binding must show `127.0.0.1`. Do not expose the daemon or
database beyond localhost; this rollout intentionally has no TLS or network
authentication.

## 4. Activate Hermes

`memoryd install` copies the bundled 0.3.0 plugin when `~/.hermes` exists.
Activate the external provider and begin a new Hermes session:

```bash
hermes config set memory.provider memoryd
hermes memoryd config
memoryd status
hermes memoryd status
```

Both status commands must be healthy. The Hermes report must show zero dead
letters and no durability fault. Incoming or processing work must drain to
zero after the daemon becomes reachable.

## 5. Disposable integration and restore drill

Run this drill from the `v0.3.0` checkout. It uses an isolated Python virtual
environment, database container, daemon port, memory home, and Hermes home.
It does not touch the production database or archive.

```bash
set -euo pipefail
cd /tmp/memoryd-v0.3.0
python3 -m venv .drill-venv
. .drill-venv/bin/activate
python -m pip install .

DRILL_ROOT="$(mktemp -d)"
DRILL_DB=memoryd-v030-drill-db
DRILL_PASSWORD="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export MEMORYD_HOME="$DRILL_ROOT/home"
export HERMES_HOME="$DRILL_ROOT/hermes"
export MEMORYD_PORT=17437
export MEMORYD_DSN="postgresql://postgres:${DRILL_PASSWORD}@127.0.0.1:55432/memoryd_drill"
mkdir -m 700 "$HERMES_HOME"

cleanup() {
  test -z "${DAEMON_PID:-}" || kill "$DAEMON_PID" 2>/dev/null || true
  test -z "${RESTORE_PID:-}" || kill "$RESTORE_PID" 2>/dev/null || true
  docker rm -f "$DRILL_DB" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --name "$DRILL_DB" \
  -e POSTGRES_PASSWORD="$DRILL_PASSWORD" -e POSTGRES_DB=memoryd_drill \
  -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
until python - <<'PY'
import os, psycopg
try:
    psycopg.connect(os.environ["MEMORYD_DSN"], connect_timeout=1).close()
except Exception:
    raise SystemExit(1)
PY
do sleep 1; done

python - <<'PY'
import os
from memoryd.cli import apply_migrations
print(apply_migrations(os.environ["MEMORYD_DSN"]))
PY
python -m memoryd serve >"$DRILL_ROOT/daemon.log" 2>&1 &
DAEMON_PID=$!
until curl -fsS http://127.0.0.1:17437/health >/dev/null; do sleep 1; done

python scripts/test_durable_capture.py
python scripts/test_hermes_spool.py
python scripts/test_bitter_lesson.py
python scripts/test_hermes_contract.py
python scripts/smoke_test.py
python scripts/test_extract.py
python scripts/test_vector.py
python scripts/test_hermes.py

kill "$DAEMON_PID"; wait "$DAEMON_PID" || true; unset DAEMON_PID
SNAPSHOT="$(python -m memoryd backup create --output "$DRILL_ROOT/backups" \
  | sed -n 's/^created //p')"
test -n "$SNAPSHOT"
python -m memoryd backup verify "$SNAPSHOT"

docker exec "$DRILL_DB" createdb -U postgres memoryd_drill_restore
RESTORE_HOME="$DRILL_ROOT/restored-home"
RESTORE_DSN="postgresql://postgres:${DRILL_PASSWORD}@127.0.0.1:55432/memoryd_drill_restore"
python -m memoryd backup restore "$SNAPSHOT" \
  --dsn "$RESTORE_DSN" --home "$RESTORE_HOME"
env -u MEMORYD_DSN MEMORYD_HOME="$RESTORE_HOME" MEMORYD_PORT=17438 \
  python -m memoryd serve >"$DRILL_ROOT/restored-daemon.log" 2>&1 &
RESTORE_PID=$!
until curl -fsS http://127.0.0.1:17438/health >/dev/null; do sleep 1; done
env -u MEMORYD_DSN MEMORYD_HOME="$RESTORE_HOME" MEMORYD_PORT=17438 \
  python -m memoryd doctor
kill "$RESTORE_PID"; wait "$RESTORE_PID" || true; unset RESTORE_PID
```

Keep `$DRILL_ROOT` and the container until every assertion passes. Remove them
only after recording the test output. Production promotion still requires the
[14-day scorecard](CANARY_SCORECARD.md).

## 6. First production snapshot

The backup command requires the daemon to be stopped. Always restart it, even
when creation or verification fails:

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

## Rollback without evidence loss

If any rollout or canary gate fails, stop external recall and writes first:

```bash
hermes config set memory.provider ''
hermes memory status
systemctl --user stop memoryd.service
```

Do not uninstall memoryd, remove the Docker container or volume, delete queue
jobs, edit dead letters, prune the archive, restore over the live database, or
reuse request IDs. Preserve at least these artifacts before diagnosis:

- `$HERMES_HOME/spool/memoryd/`, including incoming, processing, dead-letter,
  identity reservations, and fault state
- `~/memory/spool/`, `~/memory/archive/`, `~/memory/backups/`, and
  `~/memory/config.json`
- the `memoryd-pgvector` container metadata and its persistent volume
- `journalctl --user -u memoryd.service` and both status-command outputs

Copy artifacts only to owner-readable storage. Keep the database and archive
paired. Diagnose and repair in a cloned database/home, then restart the entire
14-day/200-turn canary from day zero.
