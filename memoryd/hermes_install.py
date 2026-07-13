"""Read-only preflight helpers for the guided Hermes installer."""

from __future__ import annotations

import getpass
import json
import math
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import backup, cli
from .embed import VoyageEmbedder
from .hermes_compat import HermesTarget, resolve_hermes_home
from .llm import OpenAIChatClient


class HermesInstallError(RuntimeError):
    """A safe, operator-facing guided-install error."""


@dataclass(frozen=True)
class ProviderCredentials:
    openrouter_key: str
    voyage_key: str

    def __repr__(self) -> str:
        return "ProviderCredentials(openrouter_key=<redacted>, voyage_key=<redacted>)"


_KEY_NAMES = ("OPENROUTER_API_KEY", "VOYAGE_API_KEY")
_VALIDATION_ENV = ("MEMORYD_LLM", "MEMORYD_EMBED", "MEMORYD_LLM_BASE", *_KEY_NAMES)
_INSTALL_ENV = ("HERMES_HOME", *_KEY_NAMES, "MEMORYD_LLM", "MEMORYD_EMBED")


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
