"""Read-only preflight helpers for the guided Hermes installer."""

from __future__ import annotations

import contextlib
import errno
import getpass
import io
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import time
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
_VALIDATION_ENV = ("MEMORYD_LLM", "MEMORYD_EMBED", "MEMORYD_LLM_BASE", *_KEY_NAMES)
_INSTALL_ENV = ("HERMES_HOME", *_KEY_NAMES, "MEMORYD_LLM", "MEMORYD_EMBED")
_PROVIDER_PATTERN = r"[a-z0-9][a-z0-9_-]{0,63}"
_PROVIDER_NAME = re.compile(_PROVIDER_PATTERN)
_PLUGIN_URL = "http://127.0.0.1:7437"
_SPOOL_DRAIN_TIMEOUT = 15.0
_SPOOL_POLL_INTERVAL = 0.1
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
    if not isinstance(dsn, str) or not dsn:
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


def install_hermes_core(
    target: HermesTarget, credentials: ProviderCredentials,
) -> Path:
    """Install memoryd for one validated Hermes target and verify its backup."""
    previous = {name: os.environ[name] for name in _INSTALL_ENV if name in os.environ}
    failure: str | None = None
    snapshot: Path | None = None
    try:
        try:
            current_root, current_home = resolve_hermes_home()
            if current_root != target.root or current_home != target.home:
                failure = "The authoritative Hermes target changed during revalidation."
            else:
                classify_memory_home(cli._home())
        except (Exception, SystemExit):
            failure = "The Hermes target or memoryd home failed safety revalidation."

        if failure is None:
            try:
                os.environ.update(
                    {
                        "HERMES_HOME": os.fspath(target.home),
                        "OPENROUTER_API_KEY": credentials.openrouter_key,
                        "VOYAGE_API_KEY": credentials.voyage_key,
                        "MEMORYD_LLM": "openrouter",
                        "MEMORYD_EMBED": "voyage",
                    }
                )
                if cli.install(cli._InstallOptions(hermes_home=target.home)) != 0:
                    failure = "Hermes core installation status validation failed."
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


def guided_hermes_install() -> int:
    """Run the complete interactive Hermes installation workflow."""
    credentials: ProviderCredentials | None = None
    interrupted_signal: int | None = None
    signal_can_interrupt = True
    previous_handlers: dict[int, object] = {}
    installed_handlers: set[int] = set()
    failure_message: str | None = None
    interruption_reported = False
    committed_report: str | None = None

    def interrupt(signum: int, _frame: object) -> None:
        nonlocal interrupted_signal
        if interrupted_signal is None:
            interrupted_signal = int(signum)
        if signal_can_interrupt:
            raise KeyboardInterrupt

    def report_interruption(signum: int) -> None:
        name = signal.Signals(signum).name
        print(f"Hermes guided installation interrupted ({name}).", file=sys.stderr)

    def restore_handlers() -> None:
        nonlocal signal_can_interrupt
        signal_can_interrupt = False
        interrupt_number = int(signal.SIGINT)
        terminate_number = int(signal.SIGTERM)
        try:
            if terminate_number in installed_handlers:
                signal.signal(
                    terminate_number, previous_handlers[terminate_number],
                )
                installed_handlers.remove(terminate_number)
        finally:
            if interrupt_number in installed_handlers:
                signal.signal(
                    interrupt_number, previous_handlers[interrupt_number],
                )
                installed_handlers.remove(interrupt_number)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            numeric = int(signum)
            previous_handlers[numeric] = signal.getsignal(signum)
            signal.signal(signum, interrupt)
            installed_handlers.add(numeric)

        require_guided_environment()
        target = resolve_hermes_target()
        validate_hermes_compatibility(
            target, cli._resource_dir("hermes_plugin"),
        )
        memory_home = cli._home()
        classify_memory_home(memory_home)
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
        with _activation_transaction(target):
            print(report, file=report_buffer)
            restore_handlers()
            if interrupted_signal is not None:
                raise KeyboardInterrupt
            committed_report = report_buffer.getvalue()
    except KeyboardInterrupt:
        signal_can_interrupt = False
        if interrupted_signal is None:
            interrupted_signal = int(signal.SIGINT)
    except (HermesInstallError, HermesCompatibilityError) as error:
        signal_can_interrupt = False
        failure_message = str(error)
        if credentials is not None:
            for secret in (credentials.openrouter_key, credentials.voyage_key):
                failure_message = failure_message.replace(secret, "<redacted>")
        failure_message = " ".join(failure_message.splitlines()).strip()
        if not failure_message:
            failure_message = "A required installation stage failed."
    finally:
        signal_can_interrupt = False
        if installed_handlers:
            try:
                try:
                    if interrupted_signal is not None:
                        report_interruption(interrupted_signal)
                        interruption_reported = True
                    elif failure_message is not None:
                        print(
                            f"Hermes guided installation failed: {failure_message}",
                            file=sys.stderr,
                        )
                except OSError:
                    pass
            finally:
                restore_handlers()

    if interrupted_signal is not None:
        if not interruption_reported:
            report_interruption(interrupted_signal)
        return 128 + interrupted_signal
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
