"""memoryd CLI — one command to a working install on Windows/macOS/Linux.

  memoryd install      DB (Docker pgvector) + migrations + config + Claude Code
                       hooks + autostart + Hermes plugin (when ~/.hermes exists)
  memoryd status       is it actually working? (the antidote to fail-open silence)
  memoryd serve        run the daemon in the foreground
  memoryd doctor       inspect spool and archive integrity (read-only)
  memoryd doctor --repair
                       apply conservative, evidence-preserving repairs
  memoryd review ...   human review CLI (delegates to memoryd.review)
  memoryd microsleep   nightly consolidation (normally runs on a schedule)
  memoryd backup create [--output PATH] [--retain 14]
  memoryd backup list [--output PATH]
  memoryd backup verify SNAPSHOT
  memoryd backup restore SNAPSHOT --dsn TARGET_DSN --home TARGET_HOME
  memoryd uninstall    remove hooks/autostart; data is never touched

Everything is idempotent: re-running `install` adopts what exists.
Heavy imports (psycopg via .core) happen lazily so `--help` stays instant.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CONTAINER = "memoryd-pgvector"
VOLUME = "memoryd_pgdata"
IMAGE = "pgvector/pgvector:pg16"
LEGACY_PG_PASSWORD = "memoryd"
HOOK_SENTINEL = "-m memoryd.hook"
HOOK_EVENTS = {
    "UserPromptSubmit": ("recall", 5),
    "Stop": ("capture stop", 10),
    "SessionEnd": ("capture session_end", 10),
    "PreCompact": ("capture pre_compact", 10),
}
USAGE = __doc__


# ----------------------------------------------------------------- helpers

def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def _docker(*args: str) -> tuple[int, str]:
    return _run(["docker", *args])


def _resource_dir(name: str) -> Path:
    """migrations/ or hermes_plugin: wheel ships them inside the package
    (pyproject force-include); editable installs/checkouts use the repo root."""
    p = Path(__file__).resolve().parent / name
    if p.is_dir():
        return p
    root = Path(__file__).resolve().parents[1]
    return root / ({"hermes_plugin": "hermes_plugin/memoryd"}.get(name, name))


def _home() -> Path:
    return Path(os.environ.get("MEMORYD_HOME", "~/memory")).expanduser()


def _mask(dsn: str) -> str:
    import re
    return re.sub(r"(://[^:/@]+:)[^@]+@", r"\1***@", dsn)


def _spool_counts(spool_root: Path) -> dict[str, int]:
    from .spool import is_dead_letter_sidecar

    legacy = len(list(spool_root.glob("*.json")))
    dead_letter = sum(
        1 for path in (spool_root / "dead-letter").glob("*.json")
        if not is_dead_letter_sidecar(path))
    return {
        "incoming": legacy + len(list(
            (spool_root / "incoming").glob("*.json"))),
        "processing": len(list(
            (spool_root / "processing").glob("*.json"))),
        "dead_letter": dead_letter,
    }


def _health(timeout: float = 2) -> dict | None:
    from .hook import _cfg
    port, _ = _cfg()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return None


def _pythonw() -> str:
    if sys.platform == "win32":
        pyw = Path(sys.executable).with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
    return sys.executable


# ----------------------------------------------------------------- database

def _free_port() -> int:
    for p in range(5432, 5443):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit("no free port in 5432-5442 for the postgres container")


def _container_port() -> str | None:
    code, out = _docker(
        "inspect", "-f",
        '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}', CONTAINER)
    return out if code == 0 and out else None


def _pg_ready(admin_dsn: str, wait_s: int) -> bool:
    import psycopg
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(admin_dsn, connect_timeout=2) as c:
                c.execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            time.sleep(1)
    return False


def ensure_container() -> str:
    """Adopt or (re)create the pgvector container; return the memoryd DSN."""
    import psycopg
    code, _ = _docker("info")
    if code != 0:
        raise SystemExit(
            "Docker is not running. Start Docker Desktop (and enable 'Start when "
            "you sign in'), then re-run: memoryd install\n"
            "Or point MEMORYD_DSN at your own PostgreSQL 16 + pgvector database.")

    exists, _ = _docker("inspect", CONTAINER)
    if exists == 0:
        _docker("start", CONTAINER)
        port = _container_port() or "5432"
        admin = (f"postgresql://postgres:{LEGACY_PG_PASSWORD}"
                 f"@127.0.0.1:{port}/postgres")
        if not _pg_ready(admin, 30):
            raise SystemExit(
                f"container {CONTAINER} exists but postgres is not reachable on "
                f"port {port} with the legacy credentials. Its credentials are "
                "unknown and it has not been removed. Restore a working dsn in "
                f"{_home() / 'config.json'} or set MEMORYD_DSN, then re-run.")
        with psycopg.connect(admin, autocommit=True) as c:
            has_db = c.execute(
                "SELECT 1 FROM pg_database WHERE datname='memoryd'").fetchone()
            if not has_db:
                c.execute("CREATE DATABASE memoryd")
        # Existing containers are never destroyed: the volume may contain data
        # outside memoryd that the installer cannot safely classify.
        _docker("update", "--restart", "unless-stopped", CONTAINER)
        return (f"postgresql://postgres:{LEGACY_PG_PASSWORD}"
                f"@127.0.0.1:{port}/memoryd")

    port_n = _free_port()
    password = secrets.token_urlsafe(32)
    code, out = _docker(
        "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
        "-v", f"{VOLUME}:/var/lib/postgresql/data",
        "-e", f"POSTGRES_PASSWORD={password}", "-e", "POSTGRES_DB=memoryd",
        "-p", f"127.0.0.1:{port_n}:5432", IMAGE)
    if code != 0:
        raise SystemExit(f"docker run failed: {out.replace(password, '***')}")
    admin = f"postgresql://postgres:{password}@127.0.0.1:{port_n}/postgres"
    if not _pg_ready(admin, 90):
        raise SystemExit("postgres container did not become ready within 90s")
    with psycopg.connect(admin, autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname='memoryd'").fetchone():
            c.execute("CREATE DATABASE memoryd")  # pre-existing volume without it
    return f"postgresql://postgres:{password}@127.0.0.1:{port_n}/memoryd"


def apply_migrations(dsn: str) -> list[str]:
    """Apply unapplied migrations/*.sql in sorted order, recorded in
    schema_migrations (guards against 002-after-003 constraint regressions
    and heals DBs initialized by init_db.sh, whose files are idempotent)."""
    import psycopg
    applied: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "filename TEXT PRIMARY KEY, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations")}
        for f in sorted(_resource_dir("migrations").glob("*.sql")):
            if f.name in done:
                continue
            # no params -> simple query protocol -> multi-statement scripts OK;
            # each file carries its own BEGIN/COMMIT
            conn.execute(f.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)",
                         (f.name,))
            applied.append(f.name)
    return applied


# ----------------------------------------------------------------- install steps

def write_config(dsn: str) -> Path:
    home = _home()
    home.mkdir(parents=True, exist_ok=True)
    p = home / "config.json"
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    cfg["dsn"] = dsn
    cfg.setdefault("port", 7437)
    # data dir: honor an explicit MEMORYD_HOME, else preserve any existing
    # custom "home" (relocated archive/spool/digest) — don't reset it on re-run
    if os.environ.get("MEMORYD_HOME") or "home" not in cfg:
        cfg["home"] = str(home)
    keys = ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
            "VOYAGE_API_KEY", "MEMORYD_LLM", "MEMORYD_LLM_BASE",
            "MEMORYD_LLM_MODEL", "MEMORYD_EMBED", "MEMORYD_EMBED_BASE",
            "MEMORYD_EMBED_MODEL", "MEMORYD_MODEL_PROFILE",
            "MEMORYD_EXTRACTOR_CONTRACT", "MEMORYD_SEMANTIC_POLICY",
            "MEMORYD_RECALL_POLICY",
            "MEMORYD_PACKET_COMPILER", "MEMORYD_EVAL_PROFILE")
    existing = cfg.get("env") or {}
    changed = [k for k in keys if os.environ.get(k) and existing.get(k) != os.environ[k]]
    if changed:
        env = cfg.setdefault("env", {})
        for k in changed:
            env[k] = os.environ[k]  # env wins on install so key rotation takes effect
        print(f"  config     persisted {', '.join(changed)} so scheduled runs "
              "use them; edit config.json's env map to change")
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    if sys.platform != "win32":  # the file holds API keys — keep it owner-only
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    return p


def register_claude_hooks() -> Path:
    settings = Path("~/.claude/settings.json").expanduser()
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    hooks = data.setdefault("hooks", {})
    for event, (args, timeout) in HOOK_EVENTS.items():
        # replace-in-place: drop our old entries, keep everyone else's
        entries = [e for e in hooks.get(event, [])
                   if HOOK_SENTINEL not in json.dumps(e)]
        entries.append({"hooks": [{
            "type": "command",
            "command": f'"{sys.executable}" {HOOK_SENTINEL} {args}',
            "timeout": timeout,
        }]})
        hooks[event] = entries
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return settings


def install_hermes_plugin() -> None:
    hermes = Path("~/.hermes").expanduser()
    if not hermes.is_dir():
        print("  hermes     not detected - after installing Hermes, re-run: memoryd install")
        return
    dst = hermes / "plugins" / "memory" / "memoryd"
    shutil.copytree(_resource_dir("hermes_plugin"), dst, dirs_exist_ok=True)
    cfgp = hermes / "memoryd.json"
    if not cfgp.exists():
        from .hook import _cfg
        port, _ = _cfg()
        cfgp.write_text(json.dumps({"url": f"http://127.0.0.1:{port}"}, indent=2),
                        encoding="utf-8")
    print(f"  hermes     plugin installed -> {dst}")
    print("             activate with: hermes config set memory.provider memoryd")


_SYSTEMD_SERVICE = """[Unit]
Description=memoryd memory daemon

[Service]
ExecStart={python} -m memoryd serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""

_SYSTEMD_SLEEP_SERVICE = """[Unit]
Description=memoryd nightly consolidation

[Service]
Type=oneshot
ExecStart={python} -m memoryd microsleep
"""

_SYSTEMD_SLEEP_TIMER = """[Unit]
Description=memoryd nightly consolidation

[Timer]
OnCalendar=*-*-* 03:05:00
Persistent=true

[Install]
WantedBy=timers.target
"""

_SYSTEMD_BACKUP_SERVICE = """[Unit]
Description=memoryd daily verified backup

[Service]
Type=oneshot
ExecStartPre=/usr/bin/systemctl --user stop memoryd.service
ExecStart={python} -m memoryd backup create --retain 14
ExecStopPost=/usr/bin/systemctl --user start memoryd.service
"""

_SYSTEMD_BACKUP_TIMER = """[Unit]
Description=memoryd daily verified backup

[Timer]
OnCalendar=*-*-* 02:35:00
Persistent=true

[Install]
WantedBy=timers.target
"""

_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>
    <string>{python}</string><string>-m</string><string>memoryd</string><string>{sub}</string>
  </array>
  {extra}
</dict></plist>
"""


def install_autostart() -> None:
    if sys.platform == "win32":
        py = _pythonw()
        code, _ = _run(["schtasks", "/Create", "/F", "/TN", "memoryd",
                        "/SC", "ONLOGON", "/RL", "LIMITED",
                        "/TR", f'"{py}" -m memoryd serve'])
        if code == 0:
            print("  autostart  scheduled task 'memoryd' (at logon)")
        else:
            # unelevated ONLOGON creation is often denied -> Startup-folder shim
            # (brief cmd-window flash at logon is the price of no elevation)
            shim = (Path(os.environ["APPDATA"]) /
                    "Microsoft/Windows/Start Menu/Programs/Startup/memoryd.cmd")
            shim.parent.mkdir(parents=True, exist_ok=True)
            shim.write_text(f'start "" "{py}" -m memoryd serve\n', encoding="utf-8")
            print(f"  autostart  schtasks denied; wrote startup shim {shim}")
        code, _ = _run(["schtasks", "/Create", "/F", "/TN", "memoryd-microsleep",
                        "/SC", "DAILY", "/ST", "03:05",
                        "/TR", f'"{py}" -m memoryd microsleep'])
        print("  nightly    scheduled task 'memoryd-microsleep' (03:05)" if code == 0
              else "  nightly    schtasks denied - schedule 'memoryd microsleep' yourself")
    elif sys.platform.startswith("linux"):
        unit_dir = Path("~/.config/systemd/user").expanduser()
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / "memoryd.service").write_text(
            _SYSTEMD_SERVICE.format(python=sys.executable), encoding="utf-8")
        (unit_dir / "memoryd-microsleep.service").write_text(
            _SYSTEMD_SLEEP_SERVICE.format(python=sys.executable), encoding="utf-8")
        (unit_dir / "memoryd-microsleep.timer").write_text(
            _SYSTEMD_SLEEP_TIMER, encoding="utf-8")
        (unit_dir / "memoryd-backup.service").write_text(
            _SYSTEMD_BACKUP_SERVICE.format(python=sys.executable),
            encoding="utf-8")
        (unit_dir / "memoryd-backup.timer").write_text(
            _SYSTEMD_BACKUP_TIMER, encoding="utf-8")
        _run(["systemctl", "--user", "daemon-reload"])
        code, out = _run(["systemctl", "--user", "enable", "--now",
                          "memoryd.service", "memoryd-microsleep.timer",
                          "memoryd-backup.timer"])
        print("  autostart  systemd user units enabled"
              + (" (headless box? run: loginctl enable-linger $USER)" if code == 0
                 else f" FAILED: {out}"))
    elif sys.platform == "darwin":
        la = Path("~/Library/LaunchAgents").expanduser()
        la.mkdir(parents=True, exist_ok=True)
        uid = os.getuid()
        agents = {
            "io.memoryd.daemon": _PLIST.format(
                label="io.memoryd.daemon", python=sys.executable, sub="serve",
                extra="<key>RunAtLoad</key><true/>"
                      "<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>"),
            "io.memoryd.microsleep": _PLIST.format(
                label="io.memoryd.microsleep", python=sys.executable, sub="microsleep",
                extra="<key>StartCalendarInterval</key><dict>"
                      "<key>Hour</key><integer>3</integer>"
                      "<key>Minute</key><integer>5</integer></dict>"),
        }
        for label, content in agents.items():
            plist = la / f"{label}.plist"
            plist.write_text(content, encoding="utf-8")
            _run(["launchctl", "bootout", f"gui/{uid}/{label}"])
            _run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)])
        print("  autostart  launchd agents loaded")
    else:
        print(f"  autostart  not configured for {sys.platform} - run 'memoryd serve' yourself")


def _start_daemon_now() -> None:
    if _health():
        return
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, close_fds=True)
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        cmd = [_pythonw(), "-m", "memoryd", "serve"]
        try:
            # breakaway: don't die with the launching shell's job object
            # (CI/sandboxed shells); denied on jobs that forbid breakaway
            subprocess.Popen(cmd, creationflags=flags
                             | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError:
            subprocess.Popen(cmd, creationflags=flags, **kwargs)
    else:
        kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "memoryd", "serve"], **kwargs)


# ----------------------------------------------------------------- commands

def install() -> int:
    import psycopg
    print("memoryd install")
    # BYO-Postgres short-circuit: a working preset DSN skips Docker entirely
    dsn = os.environ.get("MEMORYD_DSN")
    if not dsn:
        try:
            dsn = (json.loads((_home() / "config.json").read_text(encoding="utf-8"))
                   .get("dsn"))
        except (OSError, ValueError):
            dsn = None
    if dsn:
        try:
            with psycopg.connect(dsn, connect_timeout=3) as c:
                c.execute("SELECT 1")
            print(f"  database   using existing DSN {_mask(dsn)} (skipping Docker)")
        except Exception:  # noqa: BLE001 — stale config; fall through to Docker
            dsn = None
    if not dsn:
        dsn = ensure_container()
        print(f"  database   {_mask(dsn)}")

    applied = apply_migrations(dsn)
    total = len(list(_resource_dir("migrations").glob("*.sql")))
    print(f"  migrations {len(applied)} applied, {total} total"
          + (f" ({', '.join(applied)})" if applied else ""))
    print(f"  config     {write_config(dsn)}")
    print(f"  hooks      registered in {register_claude_hooks()}")
    install_hermes_plugin()
    install_autostart()
    _start_daemon_now()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        h = _health()
        if h and h.get("ok"):
            break
        time.sleep(1)
    print()
    return status()


def status() -> int:
    from .hook import _cfg
    port, home = _cfg()
    ok = True
    print("memoryd status")

    cfgp = home / "config.json"
    try:
        filecfg = json.loads(cfgp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        filecfg = {}
    dsn = os.environ.get("MEMORYD_DSN") or filecfg.get("dsn")
    src = "env" if os.environ.get("MEMORYD_DSN") else (
        "config.json" if filecfg.get("dsn") else "default")
    print(f"  config     dsn={_mask(dsn) if dsn else '(default)'} ({src})  home={home}")

    code, out = _docker("inspect", "-f",
                        "{{.State.Status}} restart={{.HostConfig.RestartPolicy.Name}}",
                        CONTAINER)
    print(f"  container  {CONTAINER}: {out if code == 0 else 'not found'}")

    total = len(list(_resource_dir("migrations").glob("*.sql")))
    counts: dict = {}
    reviews = None
    try:
        import psycopg
        from .core import CFG
        with psycopg.connect(CFG.dsn, connect_timeout=3) as c:
            try:
                n = len(c.execute("SELECT filename FROM schema_migrations").fetchall())
            except psycopg.Error:
                n = 0  # DB initialized by init_db.sh — no ledger table (run install)
            if n < total:
                ok = False
            print(f"  database   reachable; migrations {n}/{total} recorded")
            counts = dict(c.execute(
                "SELECT status::text, count(*) FROM memories GROUP BY status").fetchall())
            reviews = c.execute(
                "SELECT count(*) FROM review_queue WHERE NOT resolved").fetchone()[0]
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"  database   UNREACHABLE ({e})")

    h = _health()
    healthy = bool(h and h.get("ok"))
    ok = ok and healthy
    print(f"  daemon     http://127.0.0.1:{port}/health -> {'ok' if healthy else 'DOWN'}")

    settings = Path("~/.claude/settings.json").expanduser()
    try:
        sdata = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sdata = {}
    marks = [f"{ev} {'yes' if HOOK_SENTINEL in json.dumps(sdata.get('hooks', {}).get(ev, [])) else 'NO'}"
             for ev in HOOK_EVENTS]
    print(f"  hooks      {' | '.join(marks)}")

    if sys.platform == "win32":
        t1, _ = _run(["schtasks", "/Query", "/TN", "memoryd"])
        t2, _ = _run(["schtasks", "/Query", "/TN", "memoryd-microsleep"])
        shim = (Path(os.environ.get("APPDATA", "")) /
                "Microsoft/Windows/Start Menu/Programs/Startup/memoryd.cmd")
        daemon_boot = "task" if t1 == 0 else ("startup shim" if shim.exists() else "MISSING")
        print(f"  autostart  daemon: {daemon_boot} | microsleep: "
              f"{'task' if t2 == 0 else 'MISSING'}")
    elif sys.platform.startswith("linux"):
        unit = Path("~/.config/systemd/user/memoryd.service").expanduser()
        print(f"  autostart  systemd units: {'present' if unit.exists() else 'MISSING'}")
    elif sys.platform == "darwin":
        plist = Path("~/Library/LaunchAgents/io.memoryd.daemon.plist").expanduser()
        print(f"  autostart  launchd agents: {'present' if plist.exists() else 'MISSING'}")

    hp = Path("~/.hermes/plugins/memory/memoryd").expanduser()
    print(f"  hermes     {'plugin installed' if hp.is_dir() else 'not installed (~/.hermes missing)'}")

    spool_counts = _spool_counts(home / "spool")
    if spool_counts["dead_letter"]:
        ok = False
    print("  spool      "
          f"incoming={spool_counts['incoming']} "
          f"processing={spool_counts['processing']} "
          f"dead-letter={spool_counts['dead_letter']}"
          + ("  <- run `memoryd doctor`"
             if spool_counts["dead_letter"] else ""))

    mem_line = " ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none yet"
    print(f"  memories   {mem_line}"
          + (f" | pending reviews: {reviews}" if reviews is not None else ""))
    return 0 if ok else 1


def serve() -> None:
    from .core import CFG
    CFG.ensure_dirs()

    def _tty(stream) -> bool:
        try:
            return stream is not None and stream.isatty()
        except Exception:  # noqa: BLE001
            return False

    if not _tty(sys.stdout):
        # pythonw / scheduled task: print() would vanish and tracebacks with it
        log = open(CFG.home / "memoryd.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    from .server import main as server_main
    try:
        server_main()
    except OSError as e:
        # bind failed -> another instance is listening; idempotent double-start
        print(f"memoryd: not starting ({e}); another instance is likely running")
        sys.exit(0)


def uninstall() -> None:
    print("memoryd uninstall")
    settings = Path("~/.claude/settings.json").expanduser()
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        for ev in list(hooks):
            hooks[ev] = [e for e in hooks[ev] if HOOK_SENTINEL not in json.dumps(e)]
            if not hooks[ev]:
                del hooks[ev]
        settings.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"  hooks      removed from {settings}")
    except (OSError, ValueError):
        pass
    if sys.platform == "win32":
        _run(["schtasks", "/End", "/TN", "memoryd"])
        _run(["schtasks", "/Delete", "/F", "/TN", "memoryd"])
        _run(["schtasks", "/Delete", "/F", "/TN", "memoryd-microsleep"])
        shim = (Path(os.environ.get("APPDATA", "")) /
                "Microsoft/Windows/Start Menu/Programs/Startup/memoryd.cmd")
        shim.unlink(missing_ok=True)
    elif sys.platform.startswith("linux"):
        _run(["systemctl", "--user", "disable", "--now",
              "memoryd.service", "memoryd-microsleep.timer",
              "memoryd-backup.timer"])
        unit_dir = Path("~/.config/systemd/user").expanduser()
        for n in ("memoryd.service", "memoryd-microsleep.service",
                  "memoryd-microsleep.timer", "memoryd-backup.service",
                  "memoryd-backup.timer"):
            (unit_dir / n).unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
    elif sys.platform == "darwin":
        uid = os.getuid()
        for label in ("io.memoryd.daemon", "io.memoryd.microsleep"):
            _run(["launchctl", "bootout", f"gui/{uid}/{label}"])
            (Path("~/Library/LaunchAgents").expanduser() / f"{label}.plist").unlink(
                missing_ok=True)
    shutil.rmtree(Path("~/.hermes/plugins/memory/memoryd").expanduser(),
                  ignore_errors=True)
    print("  autostart  removed")
    print("  kept your data. Full purge:")
    print(f"    docker rm -f {CONTAINER} && docker volume rm {VOLUME}")
    print(f"    delete {_home()}")


def main() -> None:
    # never crash on console encoding (cp1252/cp850 can't take memory text)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "serve":
        serve()
    elif cmd == "install":
        sys.exit(install())
    elif cmd == "status":
        sys.exit(status())
    elif cmd == "doctor":
        args = sys.argv[2:]
        if args not in ([], ["--repair"]):
            print("usage: memoryd doctor [--repair]", file=sys.stderr)
            sys.exit(2)
        from .doctor import main as doctor_main
        sys.exit(doctor_main(repair=args == ["--repair"]))
    elif cmd == "review":
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        from .review import main as review_main
        review_main()
    elif cmd == "microsleep":
        from .microsleep import main as microsleep_main
        microsleep_main()
    elif cmd == "backup":
        from .backup import main as backup_main
        sys.exit(backup_main(sys.argv[2:]))
    elif cmd == "uninstall":
        uninstall()
    else:
        print(USAGE)


if __name__ == "__main__":
    main()
