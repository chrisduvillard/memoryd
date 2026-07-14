"""Read-only preflight helpers for the guided Hermes installer."""

from __future__ import annotations

import contextlib
import errno
import getpass
import hashlib
import io
import json
import math
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import backup, cli
from .embed import VoyageEmbedder
from .hermes_compat import (
    HermesCompatibilityError,
    HermesTarget,
    resolve_hermes_home,
    resolve_hermes_target,
    validate_hermes_compatibility,
)
from .llm import OpenAIChatClient


class HermesInstallError(RuntimeError):
    """A safe, operator-facing guided-install error."""


class _SpoolLockBusy(Exception):
    """Signal transient provider ownership of the durable spool lock."""


@dataclass(frozen=True)
class HermesRuntimeState:
    provider: str | None
    gateway_running: bool


@dataclass(frozen=True)
class ProviderCredentials:
    openrouter_key: str
    voyage_key: str

    def __repr__(self) -> str:
        return "ProviderCredentials(openrouter_key=<redacted>, voyage_key=<redacted>)"


_KEY_NAMES = ("OPENROUTER_API_KEY", "VOYAGE_API_KEY")
_PROVIDER_ROUTING_ENV = (
    "MEMORYD_LLM",
    "MEMORYD_LLM_BASE",
    "MEMORYD_LLM_MODEL",
    "MEMORYD_MODEL_PROFILE",
    "MEMORYD_EMBED",
    "MEMORYD_EMBED_BASE",
    "MEMORYD_EMBED_MODEL",
)
_ALTERNATE_PROVIDER_KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")
_PROVIDER_ENV = (*_PROVIDER_ROUTING_ENV, *_ALTERNATE_PROVIDER_KEYS, *_KEY_NAMES)
_VALIDATION_ENV = _PROVIDER_ENV
_INSTALL_ENV = (
    "HOME", "HERMES_HOME", "MEMORYD_HOME", "MEMORYD_DSN", "MEMORYD_PORT",
    *_PROVIDER_ENV,
)
_PROVIDER_PATTERN = r"[a-z0-9][a-z0-9_-]{0,63}"
_PROVIDER_NAME = re.compile(_PROVIDER_PATTERN)
_PLUGIN_URL = "http://127.0.0.1:7437"
_SPOOL_DRAIN_TIMEOUT = 15.0
_SPOOL_POLL_INTERVAL = 0.1
_PLUGIN_REQUIRED = frozenset(("__init__.py", "plugin.yaml", "spool.py"))
_HERMES_PROCESS_MARKERS = (
    ("_HERMES_GATEWAY", "1"),
    ("HERMES_TUI", "1"),
    ("HERMES_TUI_ACTIVE_SESSION_FILE", None),
    ("HERMES_TUI_GATEWAY_URL", None),
)
_PROVIDER_PROBE = """\
import json
import os
import re
from pathlib import Path
import yaml
provider_pattern = %r
try:
    loaded = yaml.safe_load(
        (Path(os.environ["HERMES_HOME"]) / "config.yaml").read_text(
            encoding="utf-8"
        )
    )
    config = {} if loaded is None else loaded
    if not isinstance(config, dict):
        raise ValueError
    loaded_memory = config.get("memory")
    memory = {} if loaded_memory is None else loaded_memory
    if not isinstance(memory, dict):
        raise ValueError
    provider = memory.get("provider")
    if type(provider) is str and not provider.strip():
        provider = None
    if provider is not None and (
        type(provider) is not str
        or re.fullmatch(provider_pattern, provider) is None
    ):
        raise ValueError
    encoded = json.dumps(provider)
except BaseException:
    raise SystemExit(2)
print(encoded)
""" % _PROVIDER_PATTERN
_GATEWAY_PROBE = """\
try:
    from hermes_cli.gateway import get_gateway_runtime_snapshot
    running = get_gateway_runtime_snapshot().running
    if type(running) is not bool:
        raise ValueError
except BaseException:
    raise SystemExit(2)
raise SystemExit(0 if running else 1)
"""


def _target_environment(target: HermesTarget) -> dict[str, str]:
    environment = dict(os.environ)
    environment["HERMES_HOME"] = os.fspath(target.home)
    return environment


def _capture_provider(target: HermesTarget) -> str | None:
    command = [os.fspath(target.python), "-c", _PROVIDER_PROBE]
    try:
        result = subprocess.run(
            command,
            check=False,
            env=_target_environment(target),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except BaseException:
        raise HermesInstallError("The Hermes provider state probe failed.") from None
    if result.returncode != 0 or type(result.stdout) is not str:
        raise HermesInstallError("The Hermes provider state probe failed.")
    try:
        provider = json.loads(result.stdout)
    except (TypeError, ValueError, RecursionError):
        raise HermesInstallError("The Hermes provider state is malformed.") from None
    if type(provider) is str and not provider.strip():
        provider = None
    if provider is not None and (
        type(provider) is not str or _PROVIDER_NAME.fullmatch(provider) is None
    ):
        raise HermesInstallError("The Hermes provider state is malformed.")
    return provider


def _gateway_running(target: HermesTarget) -> bool:
    command = [os.fspath(target.python), "-c", _GATEWAY_PROBE]
    try:
        result = subprocess.run(
            command,
            check=False,
            env=_target_environment(target),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except BaseException:
        raise HermesInstallError("The Hermes gateway state probe failed.") from None
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise HermesInstallError("The Hermes gateway state probe failed.")


def capture_runtime_state(target: HermesTarget) -> HermesRuntimeState:
    """Capture the selected profile's provider and gateway state without mutation."""
    return HermesRuntimeState(_capture_provider(target), _gateway_running(target))


def _run_hermes(target: HermesTarget, arguments: list[str]) -> None:
    command = [os.fspath(target.executable), *arguments]
    try:
        result = subprocess.run(
            command,
            check=False,
            env=_target_environment(target),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except BaseException:
        raise HermesInstallError("A required Hermes command could not run.") from None
    if result.returncode != 0:
        raise HermesInstallError("A required Hermes command failed.")


def _verify_plugin_config(target: HermesTarget) -> None:
    try:
        config = _read_safe_json(target.home / "memoryd.json")
    except BaseException:
        raise HermesInstallError("The Hermes plugin config is invalid.") from None
    if not isinstance(config, dict) or config.get("url") != _PLUGIN_URL:
        raise HermesInstallError("The Hermes plugin config is invalid.")


def _optional_path_stat(path: Path, *, directory: bool) -> os.stat_result | None:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise HermesInstallError("The Hermes durable spool is unreadable.") from None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(path_stat.st_mode):
        raise HermesInstallError("The Hermes durable spool has unsafe topology.")
    return path_stat


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _canonical_spool_home(target: HermesTarget) -> tuple[Path, os.stat_result]:
    home = Path(target.home)
    try:
        canonical = home.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HermesInstallError("The Hermes durable spool is unreadable.") from None
    if canonical != home:
        raise HermesInstallError("The Hermes durable spool has unsafe topology.")
    home_stat = _optional_path_stat(home, directory=True)
    if home_stat is None:
        raise HermesInstallError("The Hermes durable spool is unreadable.")
    return home, home_stat


def _require_same_directory(path: Path, expected: os.stat_result) -> None:
    current = _optional_path_stat(path, directory=True)
    if current is None or not _same_file(current, expected):
        raise HermesInstallError("The Hermes durable spool has unsafe topology.")


def _validate_lock_stat(lock_stat: os.stat_result) -> None:
    if stat.S_IMODE(lock_stat.st_mode) != 0o600:
        raise HermesInstallError("The Hermes durable spool lock is not owner-only.")
    if hasattr(os, "geteuid") and lock_stat.st_uid != os.geteuid():
        raise HermesInstallError("The Hermes durable spool lock has the wrong owner.")


def _open_spool_lock(lock_path: Path) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(3):
        existing = _optional_path_stat(lock_path, directory=False)
        if existing is not None:
            _validate_lock_stat(existing)
        created = False
        try:
            if existing is None:
                descriptor = os.open(
                    lock_path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow,
                    0o600,
                )
                created = True
            else:
                descriptor = os.open(lock_path, os.O_RDWR | nofollow)
        except FileExistsError:
            continue
        except FileNotFoundError:
            continue
        except OSError:
            raise HermesInstallError("The Hermes durable spool lock is unreadable.") from None
        try:
            if created:
                os.fchmod(descriptor, 0o600)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise HermesInstallError("The Hermes durable spool lock is unsafe.")
            _validate_lock_stat(opened)
            current = _optional_path_stat(lock_path, directory=False)
            if current is None or not _same_file(opened, current):
                raise HermesInstallError("The Hermes durable spool lock changed.")
            if existing is not None and not _same_file(existing, opened):
                raise HermesInstallError("The Hermes durable spool lock changed.")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    raise HermesInstallError("The Hermes durable spool lock changed.")


@contextlib.contextmanager
def _locked_spool(lock_path: Path):
    import fcntl

    descriptor = _open_spool_lock(lock_path)
    locked = False
    primary: BaseException | None = None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if isinstance(error, BlockingIOError) or error.errno in {
                errno.EACCES,
                errno.EAGAIN,
            }:
                raise _SpoolLockBusy from None
            raise HermesInstallError(
                "The Hermes durable spool lock failed."
            ) from None
        locked = True
        try:
            opened = os.fstat(descriptor)
        except OSError:
            raise HermesInstallError(
                "The Hermes durable spool lock failed."
            ) from None
        current = _optional_path_stat(lock_path, directory=False)
        if current is None or not _same_file(opened, current):
            raise HermesInstallError("The Hermes durable spool lock changed.")
        yield
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_failed = False
        try:
            if locked:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    cleanup_failed = True
        finally:
            try:
                os.close(descriptor)
            except OSError:
                cleanup_failed = True
        if cleanup_failed and primary is None:
            raise HermesInstallError(
                "The Hermes durable spool lock failed."
            ) from None


def _json_file_count(directory: Path) -> int:
    expected = _optional_path_stat(directory, directory=True)
    if expected is None:
        return 0
    descriptor = -1
    try:
        descriptor = os.open(
            directory,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or not _same_file(expected, opened):
            raise HermesInstallError("The Hermes durable spool directory changed.")
        count = 0
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if not entry.name.endswith(".json"):
                    continue
                try:
                    entry_stat = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(entry_stat.st_mode):
                    raise HermesInstallError(
                        "The Hermes durable spool has unsafe evidence."
                    )
                count += 1
        return count
    except FileNotFoundError:
        return 0
    except OSError:
        raise HermesInstallError("The Hermes durable spool is unreadable.") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_safe_json(
    path: Path, *, expected: os.stat_result | None = None,
) -> object:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise OSError
        if expected is not None and not _same_file(expected, opened_stat):
            raise OSError
        stream = os.fdopen(descriptor, "r", encoding="utf-8")
        descriptor = -1
        with stream:
            return json.load(stream)
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise HermesInstallError("A required JSON state file is unreadable.") from None
    finally:
        if descriptor >= 0:
            _close_descriptor(descriptor)


def _pending_spool_jobs(target: HermesTarget) -> int:
    try:
        home, home_stat = _canonical_spool_home(target)
        spool = home / "spool"
        spool_stat = _optional_path_stat(spool, directory=True)
        if spool_stat is None:
            return 0
        root = spool / "memoryd"
        root_stat = _optional_path_stat(root, directory=True)
        if root_stat is None:
            return 0
        lock_path = root / "spool.lock"
        with _locked_spool(lock_path):
            _require_same_directory(home, home_stat)
            _require_same_directory(spool, spool_stat)
            _require_same_directory(root, root_stat)
            incoming = _json_file_count(root / "incoming")
            processing = _json_file_count(root / "processing")
            dead_letters = _json_file_count(root / "dead-letter")
            if dead_letters:
                raise HermesInstallError("The Hermes durable spool has dead letters.")

            state_path = root / "state.json"
            state_stat = _optional_path_stat(state_path, directory=False)
            if state_stat is not None:
                state = _read_safe_json(state_path, expected=state_stat)
                if not isinstance(state, dict):
                    raise HermesInstallError(
                        "The Hermes durable spool state is unreadable."
                    )
                fault = state.get("durability_fault")
                if fault not in (None, ""):
                    if type(fault) is not str:
                        raise HermesInstallError(
                            "The Hermes durable spool state is unreadable."
                        )
                    raise HermesInstallError(
                        "The Hermes durable spool has a durability fault."
                    )
            return incoming + processing
    except HermesInstallError:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError, RecursionError):
        raise HermesInstallError("The Hermes durable spool is unreadable.") from None


def _wait_for_spool_drain(
    target: HermesTarget, *, timeout: float = _SPOOL_DRAIN_TIMEOUT,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            pending = _pending_spool_jobs(target)
        except _SpoolLockBusy:
            pending = None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise HermesInstallError("The Hermes durable spool did not drain.")
        if pending == 0:
            return
        time.sleep(min(_SPOOL_POLL_INTERVAL, remaining))


def _rollback_activation(
    target: HermesTarget, original: HermesRuntimeState,
) -> list[str]:
    failures: list[str] = []
    try:
        running = _gateway_running(target)
    except BaseException:
        running = None
        failures.append("gateway quiesce probe")
    if running is not False:
        try:
            _run_hermes(target, ["gateway", "stop"])
        except BaseException:
            failures.append("gateway quiesce")
        try:
            if _gateway_running(target):
                failures.append("gateway quiesce verification")
        except BaseException:
            failures.append("gateway quiesce verification")

    try:
        if original.provider is None:
            _run_hermes(target, ["memory", "off"])
        else:
            _run_hermes(
                target,
                ["config", "set", "memory.provider", original.provider],
            )
    except BaseException:
        failures.append("provider restore")
    try:
        if _capture_provider(target) != original.provider:
            failures.append("provider restore verification")
    except BaseException:
        failures.append("provider restore verification")

    if original.gateway_running:
        try:
            _run_hermes(target, ["gateway", "start"])
        except BaseException:
            failures.append("gateway restore")
        try:
            if not _gateway_running(target):
                failures.append("gateway restore verification")
        except BaseException:
            failures.append("gateway restore verification")
    else:
        try:
            if _gateway_running(target):
                failures.append("gateway restore verification")
        except BaseException:
            failures.append("gateway restore verification")
    return failures


@contextlib.contextmanager
def _activation_transaction(target: HermesTarget):
    """Activate memoryd and retain rollback ownership through the caller scope."""
    original = capture_runtime_state(target)
    stage = "transaction initialization"
    try:
        if original.gateway_running:
            stage = "gateway stop"
            _run_hermes(target, ["gateway", "stop"])
            if _gateway_running(target):
                raise HermesInstallError("The Hermes gateway did not stop.")

        stage = "provider activation"
        _run_hermes(target, ["config", "set", "memory.provider", "memoryd"])
        stage = "provider verification"
        if _capture_provider(target) != "memoryd":
            raise HermesInstallError("The Hermes provider was not activated.")

        stage = "Hermes memory status"
        _run_hermes(target, ["memory", "status"])
        stage = "Hermes memoryd config"
        _run_hermes(target, ["memoryd", "config"])
        stage = "plugin config"
        _verify_plugin_config(target)
        stage = "memoryd status"
        if cli.status() != 0:
            raise HermesInstallError("The memoryd status check failed.")
        stage = "Hermes memoryd status"
        _run_hermes(target, ["memoryd", "status"])
        stage = "spool drain"
        _wait_for_spool_drain(target)

        if original.gateway_running:
            stage = "gateway restore"
            _run_hermes(target, ["gateway", "start"])
            if not _gateway_running(target):
                raise HermesInstallError("The Hermes gateway did not restart.")
        else:
            stage = "gateway state verification"
            if _gateway_running(target):
                raise HermesInstallError("The Hermes gateway state changed.")
        stage = "final provider verification"
        if _capture_provider(target) != "memoryd":
            raise HermesInstallError("The Hermes provider state changed.")
        stage = "post-activation workflow"
        yield
    except BaseException:
        rollback_failures = _rollback_activation(target, original)
        message = f"Hermes activation failed during {stage}."
        if rollback_failures:
            message += " Rollback incomplete at: " + ", ".join(rollback_failures) + "."
        raise HermesInstallError(message) from None


def activate_and_verify(target: HermesTarget) -> None:
    """Activate memoryd transactionally and restore captured state on failure."""
    with _activation_transaction(target):
        pass


def require_guided_environment() -> None:
    """Require Linux, an interactive terminal, and a working user manager."""
    if not sys.platform.startswith("linux"):
        raise HermesInstallError("Guided installation requires Linux.")

    for name, exact in _HERMES_PROCESS_MARKERS:
        value = os.environ.get(name, "")
        if (exact is None and bool(value)) or value == exact:
            raise HermesInstallError(
                f"Hermes guided installation cannot run inside Hermes ({name})."
            )

    try:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, OSError):
        interactive = False
    if not interactive:
        raise HermesInstallError("Run guided installation from an interactive terminal (TTY).")

    probe = None
    probe_failed = False
    try:
        probe = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        probe_failed = True
    if probe_failed or probe is None or probe.returncode != 0:
        raise HermesInstallError("The systemd user manager is unavailable.")


def _operator_home_from_passwd() -> Path:
    """Return the effective Linux user's home without trusting HOME."""
    try:
        import pwd

        value = pwd.getpwuid(os.geteuid()).pw_dir
    except (ImportError, KeyError, OSError, AttributeError):
        raise HermesInstallError("The operator home could not be resolved safely.") from None
    if not isinstance(value, str) or not value:
        raise HermesInstallError("The operator home could not be resolved safely.")
    return Path(value)


def _resolved_operator_home() -> Path:
    raw_operator_home = _operator_home_from_passwd()
    if not raw_operator_home.is_absolute():
        raise HermesInstallError("The operator home could not be resolved safely.")
    try:
        operator_home = raw_operator_home.resolve(strict=True)
        operator_stat = operator_home.stat(follow_symlinks=False)
    except (OSError, RuntimeError):
        raise HermesInstallError("The operator home could not be resolved safely.") from None
    if not stat.S_ISDIR(operator_stat.st_mode):
        raise HermesInstallError("The operator home could not be resolved safely.")
    return operator_home


def _guided_hermes_environment() -> dict[str, str]:
    environment = dict(os.environ)
    if "HERMES_HOME" not in environment:
        environment["HERMES_HOME"] = os.fspath(
            _resolved_operator_home() / ".hermes"
        )
    return environment


def resolve_guided_hermes_target() -> HermesTarget:
    """Resolve Hermes without permitting HOME to select the profile root."""
    return resolve_hermes_target(_guided_hermes_environment())


def resolve_guided_hermes_home() -> tuple[Path, Path]:
    """Re-resolve the guided target using the same HOME-independent policy."""
    return resolve_hermes_home(_guided_hermes_environment())


def resolve_guided_memory_home() -> Path:
    """Resolve the one production home without trusting shell redirects."""
    if "MEMORYD_DSN" in os.environ:
        raise HermesInstallError(
            "MEMORYD_DSN must be unset for the guided Hermes installation."
        )

    operator_home = _resolved_operator_home()
    canonical = operator_home / "memory"
    try:
        canonical_stat = canonical.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise HermesInstallError("The canonical memoryd home cannot be inspected safely.") from None
    else:
        try:
            unambiguous = canonical.resolve(strict=True) == canonical
        except (OSError, RuntimeError):
            unambiguous = False
        if not unambiguous or stat.S_ISLNK(canonical_stat.st_mode):
            raise HermesInstallError("The canonical memoryd home has unsafe path topology.")

    if "MEMORYD_HOME" in os.environ:
        configured = Path(os.environ["MEMORYD_HOME"])
        if not configured.is_absolute() or configured != canonical:
            raise HermesInstallError(
                "MEMORYD_HOME must be unset or name the canonical guided memory home."
            )
    return canonical


def _is_managed_docker_dsn(value: str) -> bool:
    """Recognize only the localhost Docker DSN written by memoryd install."""
    if any(character.isspace() or ord(character) < 32 for character in value):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    return (
        parsed.scheme == "postgresql"
        and parsed.hostname == "127.0.0.1"
        and parsed.username == "postgres"
        and isinstance(parsed.password, str)
        and bool(parsed.password)
        and parsed.path == "/memoryd"
        and port is not None
        and 5432 <= port <= 5442
        and not parsed.query
        and not parsed.fragment
    )


def confirm_operator() -> None:
    """Disclose the guided install's effects and require exact confirmation."""
    print("Every Hermes chat and TUI must be closed before continuing.")
    print("Run this command from a normal terminal.")
    print("The Hermes gateway may be stopped and restarted.")
    print("Target paths will be mutated only after confirmation.")
    try:
        response = input("Type INSTALL to confirm: ")
    except (EOFError, OSError):
        raise HermesInstallError("Installation cancelled: confirmation is required.") from None
    if response != "INSTALL":
        raise HermesInstallError("Installation cancelled: exact confirmation is required.")


def classify_memory_home(home: Path) -> Literal["fresh", "managed"]:
    """Classify a memoryd home without changing it."""
    home = Path(home)
    try:
        home_stat = home.lstat()
    except FileNotFoundError:
        return "fresh"
    except OSError:
        raise HermesInstallError("The memoryd home cannot be inspected safely.") from None

    if not stat.S_ISDIR(home_stat.st_mode) or stat.S_IMODE(home_stat.st_mode) != 0o700:
        raise HermesInstallError("The memoryd home has an unsafe type or mode.")

    try:
        entries = list(home.iterdir())
    except OSError:
        raise HermesInstallError("The memoryd home cannot be inspected safely.") from None
    if not entries:
        return "fresh"

    config_path = home / "config.json"
    try:
        config_stat = config_path.lstat()
    except FileNotFoundError:
        raise HermesInstallError("The nonempty memoryd home is unknown and unsafe to manage.")
    except OSError:
        raise HermesInstallError("The managed config cannot be inspected safely.") from None
    if not stat.S_ISREG(config_stat.st_mode) or stat.S_IMODE(config_stat.st_mode) != 0o600:
        raise HermesInstallError("The managed config has an unsafe type or mode.")
    config = _read_owner_only_config(config_path)
    if not isinstance(config, dict):
        raise HermesInstallError("The managed config JSON must be an object.")

    dsn = config.get("dsn")
    if not isinstance(dsn, str) or not dsn or not _is_managed_docker_dsn(dsn):
        raise HermesInstallError("The managed config has an invalid dsn.")

    port = config.get("port")
    if type(port) is not int or port != 7437:
        raise HermesInstallError("The managed config has an invalid port.")

    configured_home = config.get("home")
    try:
        canonical_home = home.resolve(strict=True)
        configured_path = Path(configured_home) if isinstance(configured_home, str) else None
    except (OSError, RuntimeError, ValueError):
        raise HermesInstallError("The managed config has an invalid home.") from None
    if configured_path is None or not configured_path.is_absolute() or configured_path != canonical_home:
        raise HermesInstallError("The managed config home does not match this directory.")

    if "env" in config and not isinstance(config["env"], dict):
        raise HermesInstallError("The managed config env must be an object.")

    return "managed"


def validate_guided_provider_environment() -> None:
    """Refuse ambient routing that could redirect validated production keys."""
    allowed = {
        "MEMORYD_LLM": "openrouter",
        "MEMORYD_LLM_BASE": "https://openrouter.ai/api/v1",
        "MEMORYD_LLM_MODEL": "google/gemini-3.5-flash",
        "MEMORYD_MODEL_PROFILE": "openrouter",
        "MEMORYD_EMBED": "voyage",
        "MEMORYD_EMBED_MODEL": "voyage-3",
    }
    for name in _PROVIDER_ROUTING_ENV:
        value = os.environ.get(name, "")
        if not value:
            continue
        if name == "MEMORYD_EMBED_BASE" or value != allowed.get(name):
            raise HermesInstallError(
                f"{name} must be unset or use the guided production default."
            )


def collect_provider_credentials(config_path: Path) -> ProviderCredentials:
    """Collect provider keys from process env, safe config, then hidden prompts."""
    values = {key_name: os.environ.get(key_name, "") for key_name in _KEY_NAMES}
    config_env: dict[str, object] = {}
    if not all(values.values()):
        try:
            config_path_stat = Path(config_path).lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise HermesInstallError("The provider config cannot be inspected safely.") from None
        else:
            if not stat.S_ISREG(config_path_stat.st_mode) or stat.S_IMODE(config_path_stat.st_mode) != 0o600:
                raise HermesInstallError("The provider config is not owner-only.")
            config = _read_owner_only_config(Path(config_path))
            if not isinstance(config, dict):
                raise HermesInstallError("The provider config JSON must be an object.")
            env = config.get("env", {})
            if not isinstance(env, dict):
                raise HermesInstallError("The provider config env must be an object.")
            config_env = env

    for key_name, label in zip(_KEY_NAMES, ("OpenRouter", "Voyage")):
        value = values[key_name]
        config_value = config_env.get(key_name)
        if not value and isinstance(config_value, str) and config_value:
            value = config_value
        if not value:
            try:
                value = getpass.getpass(f"{label} API key: ")
            except (EOFError, OSError):
                raise HermesInstallError("A required provider credential is missing.") from None
        if not value:
            raise HermesInstallError("A required provider credential is missing.")
        values[key_name] = value

    return ProviderCredentials(values["OPENROUTER_API_KEY"], values["VOYAGE_API_KEY"])


def validate_provider_credentials(credentials: ProviderCredentials) -> None:
    """Validate both provider credentials while restoring process environment."""
    if (
        type(credentials.openrouter_key) is not str
        or not credentials.openrouter_key
        or type(credentials.voyage_key) is not str
        or not credentials.voyage_key
    ):
        raise HermesInstallError("Both provider credentials are required.")

    previous = {name: os.environ[name] for name in _VALIDATION_ENV if name in os.environ}
    try:
        environment_failed = False
        try:
            for name in _VALIDATION_ENV:
                os.environ.pop(name, None)
            os.environ.update(
                {
                    "MEMORYD_LLM": "openrouter",
                    "MEMORYD_EMBED": "voyage",
                    "MEMORYD_LLM_BASE": "https://openrouter.ai/api/v1",
                    "OPENROUTER_API_KEY": credentials.openrouter_key,
                    "VOYAGE_API_KEY": credentials.voyage_key,
                }
            )
        except (TypeError, ValueError):
            environment_failed = True
        if environment_failed:
            raise HermesInstallError("Provider credential validation could not start.")

        completion: object = None
        chat_failed = False
        try:
            chat = OpenAIChatClient("openrouter")
            completion = chat.complete(
                "Credential validation.",
                "Reply OK.",
                max_tokens=8,
            )
        except Exception:
            chat_failed = True
        if chat_failed:
            raise HermesInstallError("Chat completion credential validation failed.")
        if type(completion) is not str or not completion.strip():
            raise HermesInstallError("Chat completion credential validation returned no result.")

        embedding: object = None
        embed_failed = False
        try:
            embedder = VoyageEmbedder()
            embedding = embedder.embed(["credential validation"])
        except Exception:
            embed_failed = True
        if embed_failed:
            raise HermesInstallError("The embed credential validation failed.")

        embedding_check_failed = False
        try:
            valid_embedding = _valid_embedding(embedding)
        except Exception:
            embedding_check_failed = True
            valid_embedding = False
        if embedding_check_failed:
            raise HermesInstallError("The embed credential validation failed.")
        if not valid_embedding:
            raise HermesInstallError("The embed credential validation returned no valid result.")
    finally:
        for name in _VALIDATION_ENV:
            os.environ.pop(name, None)
        os.environ.update(previous)


def _plugin_entry_ignored(relative: Path) -> bool:
    return "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"}


def _path_exists_nofollow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise HermesInstallError("The plugin destination cannot be inspected safely.") from None
    return True


def _validate_guided_plugin_destination(target: HermesTarget) -> tuple[Path, Path]:
    home = target.home
    try:
        home_stat = home.lstat()
        home_is_canonical = home.resolve(strict=True) == home
    except (OSError, RuntimeError):
        raise HermesInstallError("The Hermes plugin destination is unsafe.") from None
    if (
        not stat.S_ISDIR(home_stat.st_mode)
        or stat.S_ISLNK(home_stat.st_mode)
        or not home_is_canonical
        or (os.name != "nt" and stat.S_IMODE(home_stat.st_mode) != 0o700)
    ):
        raise HermesInstallError("The Hermes plugin destination is unsafe.")

    plugins = home / "plugins"
    destination = plugins / "memoryd"
    config = home / "memoryd.json"
    for path, description in ((plugins, "plugin parent"), (destination, "plugin destination")):
        if not _path_exists_nofollow(path):
            continue
        try:
            value = path.lstat()
        except OSError:
            raise HermesInstallError(f"The Hermes {description} is unsafe.") from None
        if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
            raise HermesInstallError(f"The Hermes {description} has unsafe topology.")

    if _path_exists_nofollow(config):
        try:
            config_stat = config.lstat()
        except OSError:
            raise HermesInstallError("The Hermes plugin config is unsafe.") from None
        if (
            not stat.S_ISREG(config_stat.st_mode)
            or stat.S_ISLNK(config_stat.st_mode)
            or (os.name != "nt" and stat.S_IMODE(config_stat.st_mode) != 0o600)
        ):
            raise HermesInstallError("The Hermes plugin config is unsafe.")
    return plugins, destination


_DIR_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
)


def _close_fd(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _parse_fdinfo_mount_id(payload: bytes) -> int:
    if not payload or len(payload) > 16 * 1024 or b"\x00" in payload:
        raise HermesInstallError("The filesystem mount identity is unavailable.")
    values: list[int] = []
    for line in payload.splitlines():
        if not line.startswith(b"mnt_id"):
            continue
        match = re.fullmatch(rb"mnt_id:\t([1-9][0-9]*)", line)
        if match is None:
            raise HermesInstallError("The filesystem mount identity is malformed.")
        values.append(int(match.group(1)))
    if len(values) != 1:
        raise HermesInstallError("The filesystem mount identity is unavailable.")
    return values[0]


def _fd_mount_id(descriptor: int) -> int:
    fdinfo = -1
    payload = bytearray()
    try:
        fdinfo = os.open(
            f"/proc/self/fdinfo/{descriptor}",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        while True:
            chunk = os.read(fdinfo, 4096)
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > 16 * 1024:
                raise HermesInstallError("The filesystem mount identity is unavailable.")
    except HermesInstallError:
        raise
    except OSError:
        raise HermesInstallError("The filesystem mount identity is unavailable.") from None
    finally:
        if fdinfo >= 0:
            _close_fd(fdinfo)
    return _parse_fdinfo_mount_id(bytes(payload))


def _require_fd_mount(descriptor: int, mount_id: int) -> None:
    if _fd_mount_id(descriptor) != mount_id:
        raise HermesInstallError("The plugin tree crosses a filesystem mount boundary.")


def _fd_entry_stat(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise HermesInstallError("The plugin publication topology cannot be inspected safely.") from None


def _open_directory_at(
    parent_fd: int, name: str, mount_id: int | None = None,
) -> int:
    trusted_mount = _fd_mount_id(parent_fd) if mount_id is None else mount_id
    _require_fd_mount(parent_fd, trusted_mount)
    try:
        descriptor = os.open(name, _DIR_FLAGS, 0o700, dir_fd=parent_fd)
    except OSError:
        raise HermesInstallError("The plugin publication directory cannot be opened safely.") from None
    opened = os.fstat(descriptor)
    current = _fd_entry_stat(parent_fd, name)
    if current is None or not stat.S_ISDIR(opened.st_mode) or not _same_inode(opened, current):
        _close_fd(descriptor)
        raise HermesInstallError("The plugin publication directory changed during inspection.")
    try:
        _require_fd_mount(descriptor, trusted_mount)
    except BaseException:
        _close_fd(descriptor)
        raise
    return descriptor


def _open_directory_root(path: Path, expected: os.stat_result) -> int:
    try:
        descriptor = os.open(path, _DIR_FLAGS, 0o700)
        opened = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
    except (OSError, RuntimeError):
        if 'descriptor' in locals():
            _close_fd(descriptor)
        raise HermesInstallError("The plugin publication root cannot be opened safely.") from None
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not _same_inode(opened, expected)
        or not _same_inode(opened, current)
    ):
        _close_fd(descriptor)
        raise HermesInstallError("The plugin publication root changed during inspection.")
    return descriptor


def _require_root_still_open(
    path: Path, descriptor: int, mount_id: int | None = None,
) -> None:
    trusted_mount = _fd_mount_id(descriptor) if mount_id is None else mount_id
    _require_fd_mount(descriptor, trusted_mount)
    probe = -1
    try:
        current = path.stat(follow_symlinks=False)
        opened = os.fstat(descriptor)
        probe = os.open(path, _DIR_FLAGS, 0o700)
        probe_stat = os.fstat(probe)
        _require_fd_mount(probe, trusted_mount)
    except OSError:
        raise HermesInstallError("The plugin publication root changed during publication.") from None
    finally:
        if probe >= 0:
            _close_fd(probe)
    if (
        not stat.S_ISDIR(current.st_mode)
        or not _same_inode(current, opened)
        or not _same_inode(probe_stat, opened)
    ):
        raise HermesInstallError("The plugin publication root changed during publication.")


def _require_directory_entry(
    parent_fd: int, name: str, descriptor: int, mount_id: int | None = None,
) -> None:
    trusted_mount = _fd_mount_id(parent_fd) if mount_id is None else mount_id
    _require_fd_mount(parent_fd, trusted_mount)
    _require_fd_mount(descriptor, trusted_mount)
    current = _fd_entry_stat(parent_fd, name)
    opened = os.fstat(descriptor)
    if current is None or not stat.S_ISDIR(current.st_mode) or not _same_inode(current, opened):
        raise HermesInstallError("The plugin publication directory changed during publication.")
    probe = _open_directory_at(parent_fd, name, trusted_mount)
    try:
        if not _same_inode(os.fstat(probe), opened):
            raise HermesInstallError("The plugin publication directory changed during publication.")
    finally:
        _close_fd(probe)


def _require_private_directory(descriptor: int) -> None:
    value = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(value.st_mode)
        or value.st_uid != os.geteuid()
        or stat.S_IMODE(value.st_mode) != 0o700
    ):
        raise HermesInstallError("The published plugin is not owner-only.")


def _read_fd(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _fd_manifest(
    root_fd: int, *, require_private: bool, prefix: Path = Path(),
    mount_id: int | None = None,
) -> dict[str, tuple[str, str]]:
    trusted_mount = _fd_mount_id(root_fd) if mount_id is None else mount_id
    _require_fd_mount(root_fd, trusted_mount)
    if require_private:
        _require_private_directory(root_fd)
    manifest: dict[str, tuple[str, str]] = {}
    try:
        with os.scandir(root_fd) as scanner:
            names = sorted(entry.name for entry in scanner)
    except OSError:
        raise HermesInstallError("The plugin tree cannot be inspected safely.") from None
    for name in names:
        relative = prefix / name
        ignored = _plugin_entry_ignored(relative)
        value = _fd_entry_stat(root_fd, name)
        if value is None:
            raise HermesInstallError("The plugin tree changed during inspection.")
        key = relative.as_posix()
        if stat.S_ISDIR(value.st_mode):
            child = _open_directory_at(root_fd, name, trusted_mount)
            try:
                if ignored:
                    continue
                if require_private:
                    _require_private_directory(child)
                manifest[key] = ("directory", "")
                manifest.update(
                    _fd_manifest(
                        child, require_private=require_private, prefix=relative,
                        mount_id=trusted_mount,
                    )
                )
                _require_directory_entry(root_fd, name, child, trusted_mount)
            finally:
                _close_fd(child)
        elif stat.S_ISREG(value.st_mode):
            if ignored:
                continue
            descriptor = -1
            try:
                descriptor = os.open(name, _READ_FLAGS, 0o600, dir_fd=root_fd)
                opened = os.fstat(descriptor)
                current = _fd_entry_stat(root_fd, name)
                if current is None or not _same_inode(opened, value) or not _same_inode(opened, current):
                    raise HermesInstallError("The plugin file changed during inspection.")
                _require_fd_mount(descriptor, trusted_mount)
                if require_private and (
                    opened.st_uid != os.geteuid() or stat.S_IMODE(opened.st_mode) != 0o600
                ):
                    raise HermesInstallError("The published plugin is not owner-only.")
                manifest[key] = ("file", hashlib.sha256(_read_fd(descriptor)).hexdigest())
                current = _fd_entry_stat(root_fd, name)
                if current is None or not _same_inode(opened, current):
                    raise HermesInstallError("The plugin file changed during inspection.")
            except OSError:
                raise HermesInstallError("The plugin file cannot be read safely.") from None
            finally:
                if descriptor >= 0:
                    _close_fd(descriptor)
        else:
            raise HermesInstallError("The plugin tree contains a symlink or special file.")
    return manifest


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short publication write")
        view = view[written:]


def _copy_plugin_tree_fd(
    source_fd: int, destination_fd: int, prefix: Path = Path(),
    source_mount_id: int | None = None, destination_mount_id: int | None = None,
) -> None:
    trusted_source_mount = (
        _fd_mount_id(source_fd) if source_mount_id is None else source_mount_id
    )
    trusted_destination_mount = (
        _fd_mount_id(destination_fd)
        if destination_mount_id is None else destination_mount_id
    )
    _require_fd_mount(source_fd, trusted_source_mount)
    _require_fd_mount(destination_fd, trusted_destination_mount)
    try:
        with os.scandir(source_fd) as scanner:
            names = sorted(entry.name for entry in scanner)
    except OSError:
        raise HermesInstallError("The bundled plugin cannot be copied safely.") from None
    for name in names:
        relative = prefix / name
        ignored = _plugin_entry_ignored(relative)
        value = _fd_entry_stat(source_fd, name)
        if value is None:
            raise HermesInstallError("The bundled plugin changed during copying.")
        if stat.S_ISDIR(value.st_mode):
            source_child = _open_directory_at(source_fd, name, trusted_source_mount)
            destination_child = -1
            try:
                if ignored:
                    continue
                os.mkdir(name, 0o700, dir_fd=destination_fd)
                destination_child = _open_directory_at(
                    destination_fd, name, trusted_destination_mount,
                )
                os.fchmod(destination_child, 0o700)
                _copy_plugin_tree_fd(
                    source_child, destination_child, relative,
                    trusted_source_mount, trusted_destination_mount,
                )
                _require_directory_entry(
                    source_fd, name, source_child, trusted_source_mount,
                )
                _require_directory_entry(
                    destination_fd, name, destination_child,
                    trusted_destination_mount,
                )
                os.fsync(destination_child)
            except HermesInstallError:
                raise
            except OSError:
                raise HermesInstallError("The plugin stage could not be created safely.") from None
            finally:
                if destination_child >= 0:
                    _close_fd(destination_child)
                _close_fd(source_child)
            continue
        if not stat.S_ISREG(value.st_mode):
            raise HermesInstallError("The bundled plugin contains a symlink or special file.")
        if ignored:
            continue
        source_file = -1
        destination_file = -1
        try:
            source_file = os.open(name, _READ_FLAGS, 0o600, dir_fd=source_fd)
            opened = os.fstat(source_file)
            current = _fd_entry_stat(source_fd, name)
            if current is None or not _same_inode(opened, value) or not _same_inode(opened, current):
                raise HermesInstallError("The bundled plugin changed during copying.")
            _require_fd_mount(source_file, trusted_source_mount)
            destination_file = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0),
                0o600,
                dir_fd=destination_fd,
            )
            os.fchmod(destination_file, 0o600)
            _require_fd_mount(destination_file, trusted_destination_mount)
            _write_all(destination_file, _read_fd(source_file))
            os.fsync(destination_file)
            current = _fd_entry_stat(source_fd, name)
            if current is None or not _same_inode(opened, current):
                raise HermesInstallError("The bundled plugin changed during copying.")
        except HermesInstallError:
            raise
        except OSError:
            raise HermesInstallError("The bundled plugin cannot be copied safely.") from None
        finally:
            if destination_file >= 0:
                _close_fd(destination_file)
            if source_file >= 0:
                _close_fd(source_file)


def _remove_tree_at(
    parent_fd: int, name: str, mount_id: int | None = None,
) -> None:
    trusted_mount = _fd_mount_id(parent_fd) if mount_id is None else mount_id
    _require_fd_mount(parent_fd, trusted_mount)
    value = _fd_entry_stat(parent_fd, name)
    if value is None:
        return
    if not stat.S_ISDIR(value.st_mode):
        if stat.S_ISLNK(value.st_mode):
            os.unlink(name, dir_fd=parent_fd)
            return
        if not stat.S_ISREG(value.st_mode):
            raise HermesInstallError("Plugin cleanup found unsafe topology.")
        descriptor = -1
        try:
            descriptor = os.open(name, _READ_FLAGS, 0o600, dir_fd=parent_fd)
            opened = os.fstat(descriptor)
            current = _fd_entry_stat(parent_fd, name)
            if current is None or not _same_inode(opened, value) or not _same_inode(opened, current):
                raise HermesInstallError("Plugin cleanup found changed topology.")
            _require_fd_mount(descriptor, trusted_mount)
        finally:
            if descriptor >= 0:
                _close_fd(descriptor)
        os.unlink(name, dir_fd=parent_fd)
        return
    directory = _open_directory_at(parent_fd, name, trusted_mount)
    try:
        with os.scandir(directory) as scanner:
            names = sorted(entry.name for entry in scanner)
        for child_name in names:
            _remove_tree_at(directory, child_name, trusted_mount)
        _require_directory_entry(parent_fd, name, directory, trusted_mount)
    finally:
        _close_fd(directory)
    os.rmdir(name, dir_fd=parent_fd)


def _write_config_stage(
    home_fd: int, name: str, mount_id: int | None = None,
) -> bytes:
    trusted_mount = _fd_mount_id(home_fd) if mount_id is None else mount_id
    _require_fd_mount(home_fd, trusted_mount)
    payload = (json.dumps({"url": _PLUGIN_URL}, indent=2) + "\n").encode("utf-8")
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=home_fd,
        )
        os.fchmod(descriptor, 0o600)
        _require_fd_mount(descriptor, trusted_mount)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    except OSError:
        raise HermesInstallError("The Hermes plugin config could not be staged safely.") from None
    finally:
        if descriptor >= 0:
            _close_fd(descriptor)
    return payload


def _require_config_entry(
    home_fd: int, name: str, expected: bytes | None = None,
    mount_id: int | None = None,
) -> os.stat_result:
    trusted_mount = _fd_mount_id(home_fd) if mount_id is None else mount_id
    _require_fd_mount(home_fd, trusted_mount)
    value = _fd_entry_stat(home_fd, name)
    if value is None or not stat.S_ISREG(value.st_mode):
        raise HermesInstallError("The Hermes plugin config is unsafe.")
    if value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) != 0o600:
        raise HermesInstallError("The Hermes plugin config is unsafe.")
    descriptor = -1
    try:
        descriptor = os.open(name, _READ_FLAGS, 0o600, dir_fd=home_fd)
        opened = os.fstat(descriptor)
        current = _fd_entry_stat(home_fd, name)
        if current is None or not _same_inode(opened, value) or not _same_inode(opened, current):
            raise HermesInstallError("The Hermes plugin config changed during inspection.")
        _require_fd_mount(descriptor, trusted_mount)
        if expected is not None and _read_fd(descriptor) != expected:
            raise HermesInstallError("The Hermes plugin config did not verify.")
    except OSError:
        raise HermesInstallError("The Hermes plugin config cannot be read safely.") from None
    finally:
        if descriptor >= 0:
            _close_fd(descriptor)
    return value


def _unlink_config_at(
    home_fd: int, name: str, mount_id: int | None = None,
) -> None:
    trusted_mount = _fd_mount_id(home_fd) if mount_id is None else mount_id
    _require_fd_mount(home_fd, trusted_mount)
    value = _fd_entry_stat(home_fd, name)
    if value is None:
        return
    if not stat.S_ISREG(value.st_mode) and not stat.S_ISLNK(value.st_mode):
        raise HermesInstallError("Plugin config cleanup found unsafe topology.")
    os.unlink(name, dir_fd=home_fd)


def publish_guided_plugin(target: HermesTarget) -> None:
    """Publish one exact plugin/config pair through held no-follow directory fds."""
    if os.name == "nt":
        raise HermesInstallError("The guided Hermes installer is Linux-only.")

    source = cli._resource_dir("hermes_plugin")
    _validate_guided_plugin_destination(target)
    try:
        source_stat = source.lstat()
        if (
            not stat.S_ISDIR(source_stat.st_mode)
            or stat.S_ISLNK(source_stat.st_mode)
            or source.resolve(strict=True) != source
        ):
            raise HermesInstallError("The bundled plugin has unsafe path topology.")
        home_stat = target.home.stat(follow_symlinks=False)
    except HermesInstallError:
        raise
    except (OSError, RuntimeError):
        raise HermesInstallError("The plugin publication roots cannot be inspected safely.") from None

    with contextlib.ExitStack() as descriptors:
        source_fd = _open_directory_root(source, source_stat)
        descriptors.callback(_close_fd, source_fd)
        source_mount_id = _fd_mount_id(source_fd)
        source_manifest = _fd_manifest(
            source_fd, require_private=False, mount_id=source_mount_id,
        )
        if not _PLUGIN_REQUIRED <= source_manifest.keys() or any(
            source_manifest[name][0] != "file" for name in _PLUGIN_REQUIRED
        ):
            raise HermesInstallError("The bundled plugin is incomplete.")

        home_fd = _open_directory_root(target.home, home_stat)
        descriptors.callback(_close_fd, home_fd)
        home_mount_id = _fd_mount_id(home_fd)
        _require_private_directory(home_fd)

        plugins_value = _fd_entry_stat(home_fd, "plugins")
        if plugins_value is None:
            try:
                os.mkdir("plugins", 0o700, dir_fd=home_fd)
                os.fsync(home_fd)
            except OSError:
                raise HermesInstallError("The Hermes plugin parent could not be created safely.") from None
        plugins_fd = _open_directory_at(home_fd, "plugins", home_mount_id)
        descriptors.callback(_close_fd, plugins_fd)
        try:
            os.fchmod(plugins_fd, 0o700)
            os.fsync(plugins_fd)
        except OSError:
            raise HermesInstallError("The Hermes plugin parent could not be made owner-only.") from None
        _require_private_directory(plugins_fd)
        _require_directory_entry(home_fd, "plugins", plugins_fd, home_mount_id)

        old_plugin_stat = _fd_entry_stat(plugins_fd, "memoryd")
        old_plugin_manifest: dict[str, tuple[str, str]] | None = None
        if old_plugin_stat is not None:
            if not stat.S_ISDIR(old_plugin_stat.st_mode):
                raise HermesInstallError("The Hermes plugin destination has unsafe topology.")
            old_plugin_fd = _open_directory_at(
                plugins_fd, "memoryd", home_mount_id,
            )
            try:
                # A recognized rerun may be repairing stale/mode-tampered plugin
                # files. Retain their exact manifest for rollback without
                # treating the old tree as the new trusted publication.
                old_plugin_manifest = _fd_manifest(
                    old_plugin_fd, require_private=False, mount_id=home_mount_id,
                )
            finally:
                _close_fd(old_plugin_fd)

        old_config_stat = _fd_entry_stat(home_fd, "memoryd.json")
        if old_config_stat is not None:
            old_config_stat = _require_config_entry(
                home_fd, "memoryd.json", mount_id=home_mount_id,
            )

        token = secrets.token_hex(16)
        plugin_stage = f".memoryd-stage-{token}"
        plugin_rollback = f".memoryd-rollback-{token}"
        plugin_discard = f".memoryd-discard-{token}"
        config_stage = f".memoryd-config-stage-{token}"
        config_rollback = f".memoryd-config-rollback-{token}"
        config_discard = f".memoryd-config-discard-{token}"
        for parent_fd, names in (
            (plugins_fd, (plugin_stage, plugin_rollback, plugin_discard)),
            (home_fd, (config_stage, config_rollback, config_discard)),
        ):
            if any(_fd_entry_stat(parent_fd, name) is not None for name in names):
                raise HermesInstallError("The plugin publication sibling already exists.")

        deferred: BaseException | None = None
        cleanup_failures: list[BaseException] = []

        def complete(action) -> None:
            nonlocal deferred
            try:
                action()
                return
            except (KeyboardInterrupt, SystemExit) as interruption:
                if deferred is None:
                    deferred = interruption
            except BaseException as error:
                cleanup_failures.append(error)
                return
            try:
                action()
            except (KeyboardInterrupt, SystemExit) as interruption:
                if deferred is None:
                    deferred = interruption
                cleanup_failures.append(interruption)
            except BaseException as error:
                cleanup_failures.append(error)

        stage_fd = -1
        config_payload = b""
        committed = False
        prior_signal_mask: set[signal.Signals] | None = None
        try:
            try:
                os.mkdir(plugin_stage, 0o700, dir_fd=plugins_fd)
                stage_fd = _open_directory_at(
                    plugins_fd, plugin_stage, home_mount_id,
                )
                descriptors.callback(_close_fd, stage_fd)
                os.fchmod(stage_fd, 0o700)
                _copy_plugin_tree_fd(
                    source_fd, stage_fd, source_mount_id=source_mount_id,
                    destination_mount_id=home_mount_id,
                )
                os.fsync(stage_fd)
                if _fd_manifest(
                    stage_fd, require_private=True, mount_id=home_mount_id,
                ) != source_manifest:
                    raise HermesInstallError("The staged plugin manifest did not verify.")
                _require_directory_entry(
                    plugins_fd, plugin_stage, stage_fd, home_mount_id,
                )
                _require_root_still_open(source, source_fd, source_mount_id)
                if _fd_manifest(
                    source_fd, require_private=False, mount_id=source_mount_id,
                ) != source_manifest:
                    raise HermesInstallError("The bundled plugin changed during staging.")
                config_payload = _write_config_stage(
                    home_fd, config_stage, home_mount_id,
                )
                _require_config_entry(
                    home_fd, config_stage, config_payload, home_mount_id,
                )
                _require_root_still_open(target.home, home_fd, home_mount_id)
                _require_directory_entry(
                    home_fd, "plugins", plugins_fd, home_mount_id,
                )

                # Once the first visible name can move, kernel-defer SIGINT and
                # SIGTERM until the pair is either committed/cleaned or exactly
                # rolled back. Direct BaseExceptions are still handled below.
                prior_signal_mask = signal.pthread_sigmask(
                    signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM},
                )

                if old_plugin_stat is not None:
                    os.replace(
                        "memoryd", plugin_rollback,
                        src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd,
                    )
                    os.fsync(plugins_fd)
                if old_config_stat is not None:
                    os.replace(
                        "memoryd.json", config_rollback,
                        src_dir_fd=home_fd, dst_dir_fd=home_fd,
                    )
                    os.fsync(home_fd)

                os.replace(
                    plugin_stage, "memoryd",
                    src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd,
                )
                os.replace(
                    config_stage, "memoryd.json",
                    src_dir_fd=home_fd, dst_dir_fd=home_fd,
                )

                published_fd = _open_directory_at(
                    plugins_fd, "memoryd", home_mount_id,
                )
                try:
                    if _fd_manifest(
                        published_fd, require_private=True,
                        mount_id=home_mount_id,
                    ) != source_manifest:
                        raise HermesInstallError("The published plugin manifest did not verify.")
                finally:
                    _close_fd(published_fd)
                _require_config_entry(
                    home_fd, "memoryd.json", config_payload, home_mount_id,
                )
                _require_root_still_open(source, source_fd, source_mount_id)
                _require_root_still_open(target.home, home_fd, home_mount_id)
                _require_directory_entry(
                    home_fd, "plugins", plugins_fd, home_mount_id,
                )
                os.fsync(plugins_fd)
                os.fsync(home_fd)
                committed = True
            except BaseException as error:
                if committed:
                    # The pair crossed the sole commit boundary. A signal in
                    # the following bytecode gap is postcommit: retain the new
                    # pair, defer the signal, and finish both cleanup paths.
                    if isinstance(error, (KeyboardInterrupt, SystemExit)):
                        if deferred is None:
                            deferred = error
                    else:
                        cleanup_failures.append(error)
                else:
                    def restore_plugin() -> None:
                        current = _fd_entry_stat(plugins_fd, "memoryd")
                        rollback = _fd_entry_stat(plugins_fd, plugin_rollback)
                        old_visible = (
                            old_plugin_stat is not None and current is not None
                            and _same_inode(old_plugin_stat, current)
                        )
                        if not old_visible and current is not None:
                            if _fd_entry_stat(plugins_fd, plugin_discard) is not None:
                                raise HermesInstallError("Plugin rollback evidence is ambiguous.")
                            os.replace(
                                "memoryd", plugin_discard,
                                src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd,
                            )
                        if old_plugin_stat is not None and not old_visible:
                            if rollback is None or not _same_inode(old_plugin_stat, rollback):
                                raise HermesInstallError("The prior plugin rollback evidence is missing.")
                            os.replace(
                                plugin_rollback, "memoryd",
                                src_dir_fd=plugins_fd, dst_dir_fd=plugins_fd,
                            )

                    def restore_config() -> None:
                        current = _fd_entry_stat(home_fd, "memoryd.json")
                        rollback = _fd_entry_stat(home_fd, config_rollback)
                        old_visible = (
                            old_config_stat is not None and current is not None
                            and _same_inode(old_config_stat, current)
                        )
                        if not old_visible and current is not None:
                            if _fd_entry_stat(home_fd, config_discard) is not None:
                                raise HermesInstallError("Plugin config rollback evidence is ambiguous.")
                            os.replace(
                                "memoryd.json", config_discard,
                                src_dir_fd=home_fd, dst_dir_fd=home_fd,
                            )
                        if old_config_stat is not None and not old_visible:
                            if rollback is None or not _same_inode(old_config_stat, rollback):
                                raise HermesInstallError("The prior plugin config evidence is missing.")
                            os.replace(
                                config_rollback, "memoryd.json",
                                src_dir_fd=home_fd, dst_dir_fd=home_fd,
                            )

                    complete(restore_plugin)
                    complete(restore_config)
                    complete(lambda: os.fsync(plugins_fd))
                    complete(lambda: os.fsync(home_fd))
                    complete(lambda: _remove_tree_at(
                        plugins_fd, plugin_discard, home_mount_id,
                    ))
                    complete(lambda: _remove_tree_at(
                        plugins_fd, plugin_stage, home_mount_id,
                    ))
                    complete(lambda: _unlink_config_at(
                        home_fd, config_discard, home_mount_id,
                    ))
                    complete(lambda: _unlink_config_at(
                        home_fd, config_stage, home_mount_id,
                    ))
                    complete(lambda: os.fsync(plugins_fd))
                    complete(lambda: os.fsync(home_fd))

                    try:
                        restored_plugin = _fd_entry_stat(plugins_fd, "memoryd")
                        if old_plugin_stat is None:
                            if restored_plugin is not None:
                                raise HermesInstallError("The new plugin was not removed during rollback.")
                        elif restored_plugin is None or not _same_inode(old_plugin_stat, restored_plugin):
                            raise HermesInstallError("The exact prior plugin was not restored.")
                        else:
                            restored_fd = _open_directory_at(
                                plugins_fd, "memoryd", home_mount_id,
                            )
                            try:
                                if _fd_manifest(
                                    restored_fd, require_private=False,
                                    mount_id=home_mount_id,
                                ) != old_plugin_manifest:
                                    raise HermesInstallError("The exact prior plugin was not restored.")
                            finally:
                                _close_fd(restored_fd)
                        restored_config = _fd_entry_stat(home_fd, "memoryd.json")
                        if old_config_stat is None:
                            if restored_config is not None:
                                raise HermesInstallError("The new plugin config was not removed during rollback.")
                        elif restored_config is None or not _same_inode(old_config_stat, restored_config):
                            raise HermesInstallError("The exact prior plugin config was not restored.")
                        _require_root_still_open(
                            target.home, home_fd, home_mount_id,
                        )
                        _require_directory_entry(
                            home_fd, "plugins", plugins_fd, home_mount_id,
                        )
                    except BaseException as verification_error:
                        cleanup_failures.append(verification_error)

                    if cleanup_failures:
                        raise HermesInstallError(
                            "Plugin publication failed and rollback is incomplete; named evidence was preserved."
                        ) from None
                    if deferred is not None:
                        raise deferred
                    if isinstance(error, (HermesInstallError, KeyboardInterrupt, SystemExit)):
                        raise error
                    raise HermesInstallError(
                        "Plugin publication failed; the prior plugin and config were restored."
                    ) from None

            assert committed
            complete(lambda: _remove_tree_at(
                plugins_fd, plugin_rollback, home_mount_id,
            ))
            complete(lambda: _unlink_config_at(
                home_fd, config_rollback, home_mount_id,
            ))
            complete(lambda: os.fsync(plugins_fd))
            complete(lambda: os.fsync(home_fd))
            if cleanup_failures:
                raise HermesInstallError(
                    "The exact plugin/config pair was published but named cleanup evidence remains."
                ) from None
            if deferred is not None:
                raise deferred
        finally:
            # Restoring the exact prior mask may deliver a pending signal here,
            # after the visible pair and both cleanup/fsync paths are coherent.
            if prior_signal_mask is not None:
                signal.pthread_sigmask(signal.SIG_SETMASK, prior_signal_mask)


def install_hermes_core(
    target: HermesTarget, credentials: ProviderCredentials,
) -> Path:
    """Install memoryd for one validated Hermes target and verify its backup."""
    previous = {name: os.environ[name] for name in _INSTALL_ENV if name in os.environ}
    failure: str | None = None
    snapshot: Path | None = None
    try:
        operator_home = _resolved_operator_home()
        for name in _INSTALL_ENV:
            os.environ.pop(name, None)
        os.environ.update(
            {
                "HOME": os.fspath(operator_home),
                "HERMES_HOME": os.fspath(target.selector or target.home),
                "MEMORYD_HOME": os.fspath(operator_home / "memory"),
                "OPENROUTER_API_KEY": credentials.openrouter_key,
                "VOYAGE_API_KEY": credentials.voyage_key,
                "MEMORYD_LLM": "openrouter",
                "MEMORYD_EMBED": "voyage",
            }
        )
        try:
            current_root, current_home = resolve_guided_hermes_home()
            guided_memory_home = resolve_guided_memory_home()
            if current_root != target.root or current_home != target.home:
                failure = "The authoritative Hermes target changed during revalidation."
            else:
                classify_memory_home(guided_memory_home)
        except (Exception, SystemExit):
            failure = "The Hermes target or memoryd home failed safety revalidation."

        if failure is None:
            try:
                os.environ["HERMES_HOME"] = os.fspath(target.home)
                os.environ["MEMORYD_HOME"] = os.fspath(guided_memory_home)
                options = cli._InstallOptions(
                    hermes_home=target.home, publish_hermes_plugin=False,
                )
                if cli.install(options) != 0:
                    failure = "Hermes core installation status validation failed."
                else:
                    publish_guided_plugin(target)
            except (Exception, SystemExit):
                failure = "Hermes core installation failed; artifacts were preserved."

        before: set[Path] = set()
        if failure is None:
            try:
                before = {row.path for row in backup.list_backups()}
            except (Exception, SystemExit):
                failure = "The existing backup listing could not be inspected safely."

        if failure is None:
            try:
                code, _detail = cli._run(
                    ["systemctl", "--user", "start", "--wait",
                     "memoryd-backup-initial.service"],
                    timeout=660,
                )
                if code != 0:
                    failure = "The initial backup service failed; artifacts were preserved."
            except (Exception, SystemExit):
                failure = "The initial backup service failed; artifacts were preserved."

        if failure is None:
            try:
                after = {row.path for row in backup.list_backups()}
                created = after - before
                if not before <= after:
                    failure = (
                        "The initial backup did not preserve existing backup "
                        "evidence."
                    )
                elif len(created) != 1:
                    failure = "The initial backup did not create exactly one new snapshot."
                else:
                    snapshot = created.pop()
            except (Exception, SystemExit):
                failure = "The new backup listing could not be inspected safely."

        if failure is None and snapshot is not None:
            try:
                verification = backup.verify_snapshot(snapshot)
                if not verification.ok:
                    failure = "The initial backup verification failed; artifacts were preserved."
            except (Exception, SystemExit):
                failure = "The initial backup verification failed; artifacts were preserved."

        if failure is None:
            try:
                if not cli._wait_for_healthy_daemon():
                    failure = "memoryd did not become healthy after the backup service restart."
            except (Exception, SystemExit):
                failure = "memoryd did not become healthy after the backup service restart."
    finally:
        for name in _INSTALL_ENV:
            os.environ.pop(name, None)
        os.environ.update(previous)

    if failure is not None:
        raise HermesInstallError(failure)
    assert snapshot is not None
    return snapshot


@contextlib.contextmanager
def _guided_activation_environment(
    target: HermesTarget,
    memory_home: Path,
    credentials: ProviderCredentials,
):
    names = (
        "HOME", "HERMES_HOME", "MEMORYD_HOME", "MEMORYD_DSN", "MEMORYD_PORT",
        *_PROVIDER_ENV,
    )
    previous = {name: os.environ[name] for name in names if name in os.environ}
    try:
        for name in names:
            os.environ.pop(name, None)
        os.environ.update(
            {
                "HOME": os.fspath(_resolved_operator_home()),
                "HERMES_HOME": os.fspath(target.home),
                "MEMORYD_HOME": os.fspath(memory_home),
                "OPENROUTER_API_KEY": credentials.openrouter_key,
                "VOYAGE_API_KEY": credentials.voyage_key,
                "MEMORYD_LLM": "openrouter",
                "MEMORYD_EMBED": "voyage",
            }
        )
        yield
    finally:
        for name in names:
            os.environ.pop(name, None)
        os.environ.update(previous)


class _GuidedSignalFence:
    """Own SIGINT/SIGTERM until one explicit pending-snapshot boundary."""

    watched = frozenset((signal.SIGINT, signal.SIGTERM))
    ordered = (signal.SIGINT, signal.SIGTERM)

    def __init__(self) -> None:
        self.previous_handlers: dict[int, object] = {}
        self.installed_handlers: set[int] = set()
        self.prior_mask: set[signal.Signals] | None = None
        self.first_signal: int | None = None
        self.blocked = False
        self.committed = False
        self.owns_boundary = False

    def _record(self, signum: int) -> None:
        if self.first_signal is None:
            self.first_signal = int(signum)

    def _handler(self, signum: int, _frame: object) -> None:
        self._record(signum)
        self.block()
        raise KeyboardInterrupt

    def start(self) -> None:
        try:
            prior = signal.pthread_sigmask(signal.SIG_BLOCK, self.watched)
        except (OSError, ValueError, AttributeError):
            raise HermesInstallError(
                "The POSIX signal safety boundary is unavailable."
            ) from None
        self.prior_mask = set(prior)
        self.blocked = True
        if self.prior_mask & self.watched:
            signal.pthread_sigmask(signal.SIG_SETMASK, self.prior_mask)
            self.blocked = False
            raise HermesInstallError(
                "SIGINT and SIGTERM must be unblocked before guided installation."
            )
        self.owns_boundary = True
        try:
            for signum in self.ordered:
                numeric = int(signum)
                self.previous_handlers[numeric] = signal.getsignal(signum)
                signal.signal(signum, self._handler)
                self.installed_handlers.add(numeric)
            signal.pthread_sigmask(signal.SIG_SETMASK, self.prior_mask)
            self.blocked = False
        except BaseException:
            self.block()
            self.restore_handlers()
            self.release()
            raise HermesInstallError(
                "The guided signal safety boundary could not be installed."
            ) from None

    def block(self) -> None:
        if not self.owns_boundary or self.prior_mask is None or self.blocked:
            return
        signal.pthread_sigmask(signal.SIG_BLOCK, self.watched)
        self.blocked = True

    def restore_handlers(self) -> None:
        if not self.owns_boundary:
            return
        self.block()
        failures: list[BaseException] = []
        for signum in reversed(self.ordered):
            numeric = int(signum)
            if numeric not in self.installed_handlers:
                continue
            try:
                signal.signal(signum, self.previous_handlers[numeric])
            except KeyboardInterrupt as error:
                self._record(numeric)
                try:
                    signal.signal(signum, self.previous_handlers[numeric])
                except BaseException as retry_error:
                    failures.append(retry_error)
                    continue
            except BaseException as error:
                failures.append(error)
                continue
            self.installed_handlers.remove(numeric)
        if failures or self.installed_handlers:
            raise HermesInstallError(
                "The original signal handlers could not be restored."
            )

    def _pending_snapshot(self) -> set[signal.Signals]:
        try:
            return set(signal.sigpending()) & self.watched
        except (OSError, ValueError, AttributeError):
            raise HermesInstallError(
                "The pending signal boundary could not be inspected."
            ) from None

    def _consume(self, pending: set[signal.Signals]) -> None:
        for signum in self.ordered:
            if signum not in pending:
                continue
            try:
                received = signal.sigwait({signum})
            except (OSError, ValueError, AttributeError):
                raise HermesInstallError(
                    "A pending installer signal could not be consumed safely."
                ) from None
            self._record(int(received))

    def linearize_commit(self) -> None:
        """Classify the one kernel pending snapshot as precommit."""
        self.block()
        pending = self._pending_snapshot()
        self._consume(pending)
        if self.first_signal is not None:
            raise KeyboardInterrupt
        self.committed = True

    def finish_abort(self) -> None:
        """Consume the abort-boundary snapshot before original semantics resume."""
        if not self.owns_boundary:
            return
        self.block()
        self._consume(self._pending_snapshot())

    def release(self) -> None:
        if not self.owns_boundary or self.prior_mask is None or not self.blocked:
            return
        prior = self.prior_mask
        self.blocked = False
        self.owns_boundary = False
        signal.pthread_sigmask(signal.SIG_SETMASK, prior)


def guided_hermes_install() -> int:
    """Run the complete interactive Hermes installation workflow."""
    credentials: ProviderCredentials | None = None
    fence = _GuidedSignalFence()
    failure_message: str | None = None
    interruption_reported = False
    committed_report: str | None = None
    committed = False
    activation_environment = None
    activation_environment_entered = False

    def report_interruption(signum: int) -> None:
        name = signal.Signals(signum).name
        print(f"Hermes guided installation interrupted ({name}).", file=sys.stderr)

    try:
        fence.start()
        require_guided_environment()
        memory_home = resolve_guided_memory_home()
        target = resolve_guided_hermes_target()
        validate_hermes_compatibility(
            target, cli._resource_dir("hermes_plugin"),
        )
        classify_memory_home(memory_home)
        validate_guided_provider_environment()
        confirm_operator()
        credentials = collect_provider_credentials(memory_home / "config.json")
        validate_provider_credentials(credentials)
        snapshot = install_hermes_core(target, credentials)

        report = "\n".join(
            (
                f"Authoritative Hermes profile: {target.home}",
                "memoryd daemon: http://127.0.0.1:7437",
                f"Verified initial snapshot: {snapshot}",
                "Four healthy checks passed: Hermes memory status, Hermes memoryd config, "
                "memoryd status, Hermes memoryd status.",
                "Restored prior gateway state.",
                "Start or continue the existing 14-day/200-turn canary before promotion.",
            )
        )
        for secret in (credentials.openrouter_key, credentials.voyage_key):
            report = report.replace(secret, "<redacted>")
        report_buffer = io.StringIO()
        fence.block()
        activation_environment = _guided_activation_environment(
            target, memory_home, credentials,
        )
        activation_environment.__enter__()
        activation_environment_entered = True
        with _activation_transaction(target):
            print(report, file=report_buffer)
            committed_report = report_buffer.getvalue()
            try:
                activation_environment.__exit__(None, None, None)
            finally:
                activation_environment_entered = False
            fence.restore_handlers()
            fence.linearize_commit()
        committed = True
    except KeyboardInterrupt:
        if fence.first_signal is None:
            fence._record(int(signal.SIGINT))
    except (HermesInstallError, HermesCompatibilityError) as error:
        failure_message = str(error)
        if credentials is not None:
            for secret in (credentials.openrouter_key, credentials.voyage_key):
                failure_message = failure_message.replace(secret, "<redacted>")
        failure_message = " ".join(failure_message.splitlines()).strip()
        if not failure_message:
            failure_message = "A required installation stage failed."
    finally:
        if not committed:
            fence.block()
            if activation_environment_entered and activation_environment is not None:
                try:
                    activation_environment.__exit__(None, None, None)
                except BaseException:
                    if failure_message is None and fence.first_signal is None:
                        failure_message = "The canonical installation environment could not be restored."
                finally:
                    activation_environment_entered = False
            try:
                try:
                    if fence.first_signal is not None:
                        report_interruption(fence.first_signal)
                        interruption_reported = True
                    elif failure_message is not None:
                        print(
                            f"Hermes guided installation failed: {failure_message}",
                            file=sys.stderr,
                        )
                except (OSError, KeyboardInterrupt):
                    pass
            finally:
                try:
                    fence.restore_handlers()
                    fence.finish_abort()
                except HermesInstallError as cleanup_error:
                    if failure_message is None and fence.first_signal is None:
                        failure_message = str(cleanup_error)
                finally:
                    fence.release()

    # This is deliberately outside the guided exception mapping. Signals
    # arriving after the pending snapshot resume the caller's exact original
    # disposition and cannot be misreported as a failed installation.
    if committed:
        fence.release()

    if fence.first_signal is not None:
        if not interruption_reported:
            report_interruption(fence.first_signal)
        return 128 + fence.first_signal
    if failure_message is not None:
        return 1
    assert committed_report is not None
    print(committed_report, end="")
    return 0


def _read_owner_only_config(config_path: Path) -> object:
    """Read JSON only from a real owner-only regular file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = -1
    open_failed = False
    try:
        descriptor = os.open(config_path, flags)
    except OSError:
        open_failed = True
    if open_failed:
        raise HermesInstallError("The config JSON cannot be read safely.")

    stat_failed = False
    try:
        opened_stat = os.fstat(descriptor)
    except OSError:
        stat_failed = True
    if stat_failed:
        _close_descriptor(descriptor)
        raise HermesInstallError("The config JSON cannot be read safely.")
    if not stat.S_ISREG(opened_stat.st_mode) or stat.S_IMODE(opened_stat.st_mode) != 0o600:
        _close_descriptor(descriptor)
        raise HermesInstallError("The config is not an owner-only regular file.")

    parsed: object = None
    read_failed = False
    try:
        stream = os.fdopen(descriptor, "r", encoding="utf-8")
        descriptor = -1
        with stream:
            parsed = json.load(stream)
    except (OSError, UnicodeError, ValueError, RecursionError):
        read_failed = True
    finally:
        if descriptor >= 0:
            _close_descriptor(descriptor)
    if read_failed:
        raise HermesInstallError("The config JSON cannot be read safely.")
    return parsed


def _close_descriptor(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _valid_embedding(value: object) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 1:
        return False
    vector = value[0]
    if not isinstance(vector, (list, tuple)) or not vector:
        return False
    for component in vector:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            return False
        if not math.isfinite(component):
            return False
    return True
