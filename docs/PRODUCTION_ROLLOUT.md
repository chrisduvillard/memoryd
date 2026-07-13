# Production Linux rollout

This runbook installs one clean, authoritative memoryd 0.3.0 instance beside
Hermes on the Linux laptop. It does not read, repair, delete, or migrate the
Windows installation or its data. Run every command below from a normal user
shell on Linux, never from inside a Hermes tool call.

## Fixed release inputs

- memoryd and the bundled Hermes plugin: `v0.3.0`
- Hermes Agent package: `hermes-agent==0.16.0`
- Hermes source tag: `v2026.6.5`
- Hermes resolved commit:
  `3c231eb3979ab9c57d5cd6d02f1d577a3b718b43`
- memoryd API: `http://127.0.0.1:7437`
- memory home: exactly `~/memory`
- database: Docker PostgreSQL 16 with pgvector, bound to `127.0.0.1`

The [official Hermes release](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.6.5)
names package version 0.16.0 and source tag `v2026.6.5`. Do not substitute
upstream `main` during this rollout.

## 1. Preflight, select the Hermes profile, and pin Hermes

Install Docker, Python 3.11 or 3.13, `pipx`, `git`, `curl`, and PostgreSQL
client tools. Select one authoritative Hermes profile before running either
installer. Every later command must run from a shell that exports this same
absolute `HERMES_HOME`.

```bash
set -euo pipefail
umask 077
docker version
python3 --version
pipx --version
git --version
curl --version
psql --version
hermes --version

HERMES_ROOT="$(realpath -m "${HERMES_HOME:-$HOME/.hermes}")"
# Hermes treats an explicit .../profiles/<name> path as authoritative. When
# given the root, resolve its sticky active profile now so memoryd and every
# later Hermes command address the same directory.
if [[ "$(basename "$(dirname "$HERMES_ROOT")")" == profiles ]]; then
  export HERMES_HOME="$HERMES_ROOT"
elif [[ -s "$HERMES_ROOT/active_profile" ]]; then
  IFS= read -r HERMES_PROFILE <"$HERMES_ROOT/active_profile"
  if [[ "$HERMES_PROFILE" == default ]]; then
    export HERMES_HOME="$HERMES_ROOT"
  elif [[ "$HERMES_PROFILE" =~ ^[a-z0-9][a-z0-9_-]{0,63}$ ]]; then
    export HERMES_HOME="$HERMES_ROOT/profiles/$HERMES_PROFILE"
  else
    echo 'Invalid Hermes active_profile value.' >&2; exit 1
  fi
else
  export HERMES_HOME="$HERMES_ROOT"
fi
case "$HERMES_HOME" in /*) ;; *) echo 'HERMES_HOME must be absolute' >&2; exit 1;; esac
install -d -m 700 "$HERMES_HOME"
test "$(stat -c '%a' "$HERMES_HOME")" = 700

# A production rollout always uses ~/memory. An inherited override is a
# configuration error, even when it is the empty string.
if [[ -n ${MEMORYD_HOME+x} ]]; then
  echo 'Unset MEMORYD_HOME; production uses the clean default ~/memory.' >&2
  exit 1
fi
test ! -e "$HOME/memory" || {
  echo '~/memory already exists; stop and investigate it without modifying it.' >&2
  exit 1
}
```

Resolve the interpreter behind the active `hermes` command and verify the
installed distribution. If either assertion fails, reinstall the exact
resolved commit and repeat the check; do not use `hermes update`.

```bash
HERMES_BIN="$(readlink -f "$(command -v hermes)")"
export HERMES_PY="$(sed -n '1s/^#!//p' "$HERMES_BIN")"
test -x "$HERMES_PY"

if ! "$HERMES_PY" - <<'PY'
from importlib.metadata import version
actual = version("hermes-agent")
assert actual == "0.16.0", f"expected hermes-agent 0.16.0, got {actual}"
print(f"hermes-agent {actual}")
PY
then
  pipx install --force --python python3.13 \
    'git+https://github.com/NousResearch/hermes-agent.git@3c231eb3979ab9c57d5cd6d02f1d577a3b718b43'
  hash -r
  HERMES_BIN="$(readlink -f "$(command -v hermes)")"
  export HERMES_PY="$(sed -n '1s/^#!//p' "$HERMES_BIN")"
  "$HERMES_PY" -c \
    'from importlib.metadata import version; assert version("hermes-agent") == "0.16.0"'
fi
```

Use Python 3.11 in that remediation command if Python 3.13 is unavailable.
For a source/editable Hermes installation, additionally require its checkout
HEAD to equal the resolved commit above.

Check out memoryd's tagged release into a new owner-private directory and run
the immutable contract checker with the installed Hermes interpreter:

```bash
export MEMORYD_RELEASE_DIR="$HOME/.local/src/memoryd-v0.3.0"
test ! -e "$MEMORYD_RELEASE_DIR"
install -d -m 700 "$(dirname "$MEMORYD_RELEASE_DIR")"
git clone --branch v0.3.0 --depth 1 \
  https://github.com/chrisduvillard/memoryd.git "$MEMORYD_RELEASE_DIR"
cd "$MEMORYD_RELEASE_DIR"
"$HERMES_PY" scripts/check_hermes_contract.py
```

The checker must print `COMPATIBLE`.

## 2. Install memoryd in its own pipx environment

```bash
pipx install --python python3.13 \
  'git+https://github.com/chrisduvillard/memoryd.git@v0.3.0'
hash -r
memoryd --help

MEMORYD_BIN="$(readlink -f "$(command -v memoryd)")"
export MEMORYD_PY="$(sed -n '1s/^#!//p' "$MEMORYD_BIN")"
test -x "$MEMORYD_PY"
export MEMORYD_PLUGIN_SOURCE="$($MEMORYD_PY - <<'PY'
from pathlib import Path
import memoryd
source = Path(memoryd.__file__).resolve().with_name("hermes_plugin")
assert (source / "plugin.yaml").is_file(), source
print(source)
PY
)"
```

Use Python 3.11 if Python 3.13 is unavailable. Validate the installed Hermes
loader and complete lifecycle against the plugin copied from the installed
memoryd wheel. The validator owns a temporary isolated profile, starts its own
localhost probe, and must report recall, capture, pre-compress, extraction,
loader origin, and an empty durable spool.

```bash
HERMES_VALIDATION_HOME="$(mktemp -d)"
"$HERMES_PY" "$MEMORYD_RELEASE_DIR/scripts/validate_installed_hermes.py" \
  --hermes-home "$HERMES_VALIDATION_HOME" \
  --plugin-source "$MEMORYD_PLUGIN_SOURCE" \
  --expected-version 0.16.0
```

Do not continue on version drift, a checkout-origin plugin, a loader mismatch,
an uncalled lifecycle hook, a nonempty spool, or a durability fault. Preserve
the isolated profile and validator output as rollout evidence.

## 3. Supply provider secrets and install

Read secrets interactively so they never enter shell history. `memoryd
install` stores them in the owner-readable configuration used by the systemd
user service. The backup manifest records their names, never values.

```bash
read -rsp 'OpenRouter API key: ' OPENROUTER_API_KEY; printf '\n'
read -rsp 'Voyage API key: ' VOYAGE_API_KEY; printf '\n'
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

The installer must place the plugin at `$HERMES_HOME/plugins/memoryd`, never
the obsolete nested `plugins/memory/memoryd` path, and must publish exactly
the selected localhost URL in an owner-only profile configuration:

```bash
"$HERMES_PY" - <<'PY'
import json, os, stat
from pathlib import Path

home_text = os.environ["HERMES_HOME"]
home = Path(home_text).resolve()
assert home_text == str(home), (home_text, home)
assert (home / "plugins/memoryd/plugin.yaml").is_file()
assert not (home / "plugins/memory/memoryd").exists()
cfg = home / "memoryd.json"
assert json.loads(cfg.read_text(encoding="utf-8")) == {
    "url": "http://127.0.0.1:7437"
}
assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
print(f"verified Hermes profile {home}")
PY
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

## 4. Activate Hermes without interrupting a turn

First exit every active Hermes chat/TUI cleanly and wait for any in-flight
response to finish. Hermes 0.16.0 has no command that drains an interactive
chat, so this is an operator gate. The following installed-runtime probe
records whether the selected profile's gateway must be restarted:

```bash
gateway_running() {
  "$HERMES_PY" -c \
    'from hermes_cli.gateway import get_gateway_runtime_snapshot as s; raise SystemExit(0 if s().running else 1)'
}

GATEWAY_WAS_ACTIVE=0
if gateway_running; then
  GATEWAY_WAS_ACTIVE=1
  hermes gateway stop
  ! gateway_running
fi

hermes config set memory.provider memoryd
"$HERMES_PY" - <<'PY'
import os
from pathlib import Path
import yaml
cfg = yaml.safe_load((Path(os.environ["HERMES_HOME"]) / "config.yaml").read_text()) or {}
assert (cfg.get("memory") or {}).get("provider") == "memoryd", cfg.get("memory")
PY
hermes memory status
hermes memoryd config
memoryd status
hermes memoryd status

if (( GATEWAY_WAS_ACTIVE )); then
  hermes gateway start
  gateway_running
fi
```

Both memoryd status commands must be healthy. The Hermes report must show the
exact URL, zero dead letters, and no durability fault. Incoming or processing
work must drain to zero after the daemon becomes reachable. Start a new
chat/TUI only after these checks and any previously active gateway are healthy.

## 5. Disposable integration and restore drill

Run this drill from the tagged checkout. It uses an isolated Python virtual
environment, database container, daemon port, memory home, and Hermes home.
The custom `MEMORYD_HOME` below is permitted only for this disposable test; it
does not alter the production `~/memory` decision.

```bash
set -euo pipefail
umask 077
cd "$MEMORYD_RELEASE_DIR"
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
install -d -m 700 "$HERMES_HOME"

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
"$HERMES_PY" scripts/test_hermes_contract.py
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
[14-day scorecard](CANARY_SCORECARD.md), including a restore of a real
production snapshot rather than only this synthetic drill.

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

Run rollback from a separate shell, not from the gateway or a Hermes tool
call. Exit active chat/TUI sessions cleanly and wait for in-flight turns. Then
disable the external provider while memoryd is still available, verify the
selected profile is built-in-only, restart only a gateway that was previously
active, and finally stop memoryd:

```bash
set -euo pipefail
gateway_running() {
  "$HERMES_PY" -c \
    'from hermes_cli.gateway import get_gateway_runtime_snapshot as s; raise SystemExit(0 if s().running else 1)'
}

ROLLBACK_GATEWAY_WAS_ACTIVE=0
if gateway_running; then
  ROLLBACK_GATEWAY_WAS_ACTIVE=1
  hermes gateway stop
  ! gateway_running
fi

hermes memory off
"$HERMES_PY" - <<'PY'
import os
from pathlib import Path
import yaml
cfg = yaml.safe_load((Path(os.environ["HERMES_HOME"]) / "config.yaml").read_text()) or {}
assert not (cfg.get("memory") or {}).get("provider"), cfg.get("memory")
PY
hermes memory status

if (( ROLLBACK_GATEWAY_WAS_ACTIVE )); then
  hermes gateway start
  gateway_running
fi
systemctl --user stop memoryd.service
```

`hermes memory status` must report no external provider before memoryd stops.
Do not start a new chat/TUI until that is true. Do not uninstall memoryd,
remove the Docker container or volume, delete queue jobs, edit dead letters,
prune the archive, restore over the live database, or reuse request IDs.
Preserve at least these artifacts before diagnosis:

- `$HERMES_HOME/spool/memoryd/`, including incoming, processing, dead-letter,
  identity reservations, and fault state
- `~/memory/spool/`, `~/memory/archive/`, `~/memory/backups/`, and
  `~/memory/config.json`
- the `memoryd-pgvector` container metadata and its persistent volume
- `journalctl --user -u memoryd.service` and both status-command outputs

Copy artifacts only to owner-readable storage. Keep the database and archive
paired. Diagnose and repair in a cloned database/home, then restart the entire
14-day/200-turn canary from day zero.
