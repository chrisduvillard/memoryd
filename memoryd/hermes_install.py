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

from .embed import VoyageEmbedder
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
_VALIDATION_ENV = ("MEMORYD_LLM", "MEMORYD_EMBED", *_KEY_NAMES)


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

    try:
        probe = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        raise HermesInstallError("The systemd user manager is unavailable.") from None
    if probe.returncode != 0:
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
    config_env: dict[str, object] = {}
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

    values: list[str] = []
    for key_name, label in zip(_KEY_NAMES, ("OpenRouter", "Voyage")):
        value = os.environ.get(key_name, "")
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
        values.append(value)

    return ProviderCredentials(values[0], values[1])


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
        try:
            os.environ.update(
                {
                    "MEMORYD_LLM": "openrouter",
                    "MEMORYD_EMBED": "voyage",
                    "OPENROUTER_API_KEY": credentials.openrouter_key,
                    "VOYAGE_API_KEY": credentials.voyage_key,
                }
            )
        except (TypeError, ValueError):
            raise HermesInstallError("Provider credential validation could not start.") from None

        try:
            chat = OpenAIChatClient("openrouter")
            completion = chat.complete(
                "Credential validation.",
                "Reply OK.",
                max_tokens=8,
            )
        except Exception:
            raise HermesInstallError("Chat completion credential validation failed.") from None
        if type(completion) is not str or not completion.strip():
            raise HermesInstallError("Chat completion credential validation returned no result.")

        try:
            embedder = VoyageEmbedder()
            embedding = embedder.embed(["credential validation"])
        except Exception:
            raise HermesInstallError("The embed credential validation failed.") from None
        try:
            valid_embedding = _valid_embedding(embedding)
        except Exception:
            raise HermesInstallError("The embed credential validation failed.") from None
        if not valid_embedding:
            raise HermesInstallError("The embed credential validation returned no valid result.")
    finally:
        for name in _VALIDATION_ENV:
            os.environ.pop(name, None)
        os.environ.update(previous)


def _read_owner_only_config(config_path: Path) -> object:
    """Read JSON only from a real owner-only regular file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(config_path, flags)
        try:
            opened_stat = os.fstat(descriptor)
            if not stat.S_ISREG(opened_stat.st_mode) or stat.S_IMODE(opened_stat.st_mode) != 0o600:
                raise HermesInstallError("The config is not an owner-only regular file.")
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = -1
                return json.load(stream)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except HermesInstallError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise HermesInstallError("The config JSON cannot be read safely.") from None


def _valid_embedding(value: object) -> bool:
    if not isinstance(value, (list, tuple)) or not value:
        return False
    vectors = value if isinstance(value[0], (list, tuple)) else (value,)
    for vector in vectors:
        if not isinstance(vector, (list, tuple)) or not vector:
            return False
        for component in vector:
            if isinstance(component, bool) or not isinstance(component, (int, float)):
                return False
            if not math.isfinite(component):
                return False
    return True
