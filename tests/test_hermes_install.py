from __future__ import annotations

import builtins
import getpass
import inspect
import json
import os
import signal
import stat
import subprocess
import sys
import traceback
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from memoryd import backup, cli
from memoryd.hermes_compat import HermesTarget
import memoryd.hermes_install as hermes


KEY_NAMES = ("OPENROUTER_API_KEY", "VOYAGE_API_KEY")
AFFECTED_ENV = ("MEMORYD_LLM", "MEMORYD_EMBED", "MEMORYD_LLM_BASE", *KEY_NAMES)
INSTALL_ENV = ("HERMES_HOME", "MEMORYD_HOME", "OPENROUTER_API_KEY",
               "VOYAGE_API_KEY", "MEMORYD_LLM", "MEMORYD_EMBED")


def _tty(value: bool = True) -> SimpleNamespace:
    return SimpleNamespace(isatty=lambda: value)


def _safe_config(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _managed_payload(home: Path, *, include_env: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "dsn": "postgresql://postgres:test@127.0.0.1:5432/memoryd",
        "port": 7437,
        "home": str(home.resolve()),
    }
    if include_env:
        payload["env"] = {
            "OPENROUTER_API_KEY": "config-openrouter",
            "VOYAGE_API_KEY": "config-voyage",
        }
    return payload


def _operator_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    operator = tmp_path / "operator"
    operator.mkdir(mode=0o700)
    monkeypatch.setattr(
        hermes, "_operator_home_from_passwd", lambda: operator, raising=False,
    )
    return operator


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MEMORYD_HOME", "/tmp/redirected-memory-SENSITIVE"),
        (
            "MEMORYD_DSN",
            "postgresql://operator:SENSITIVE@remote.invalid/unrelated",
        ),
    ],
)
def test_guided_memory_home_rejects_ambient_redirects_without_echo(
    monkeypatch, tmp_path, name, value,
):
    _operator_home(monkeypatch, tmp_path)
    monkeypatch.delenv("MEMORYD_HOME", raising=False)
    monkeypatch.delenv("MEMORYD_DSN", raising=False)
    monkeypatch.setenv(name, value)

    with pytest.raises(hermes.HermesInstallError) as caught:
        hermes.resolve_guided_memory_home()

    assert name in str(caught.value)
    assert value not in str(caught.value)
    assert "SENSITIVE" not in str(caught.value)


def test_guided_memory_home_is_resolved_operator_memory_and_accepts_exact_override(
    monkeypatch, tmp_path,
):
    operator = _operator_home(monkeypatch, tmp_path)
    expected = operator.resolve() / "memory"
    monkeypatch.delenv("MEMORYD_DSN", raising=False)
    monkeypatch.setenv("MEMORYD_HOME", str(expected))

    assert hermes.resolve_guided_memory_home() == expected


def test_guided_hermes_target_ignores_ambient_home_without_explicit_hermes_home(
    monkeypatch, tmp_path,
):
    operator = _operator_home(monkeypatch, tmp_path)
    redirected = tmp_path / "redirected"
    redirected.mkdir(mode=0o700)
    marker = redirected / ".hermes" / "active_profile"
    marker.parent.mkdir(mode=0o700)
    marker.write_text("do-not-read", encoding="utf-8")
    monkeypatch.setenv("HOME", str(redirected))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    sentinel = object()
    captured: dict[str, str] = {}

    def resolve(environ):
        captured.update(environ)
        return sentinel

    monkeypatch.setattr(hermes, "resolve_hermes_target", resolve)

    assert hermes.resolve_guided_hermes_target() is sentinel
    assert captured["HERMES_HOME"] == str(operator.resolve() / ".hermes")
    assert captured["HOME"] == str(redirected)
    assert marker.read_text(encoding="utf-8") == "do-not-read"


def test_guided_hermes_target_preserves_explicit_hermes_home(monkeypatch, tmp_path):
    _operator_home(monkeypatch, tmp_path)
    configured = tmp_path / "selected-hermes"
    monkeypatch.setenv("HOME", str(tmp_path / "ignored-home"))
    monkeypatch.setenv("HERMES_HOME", str(configured))
    captured: dict[str, str] = {}

    def resolve(environ):
        captured.update(environ)
        return object()

    monkeypatch.setattr(hermes, "resolve_hermes_target", resolve)

    hermes.resolve_guided_hermes_target()

    assert captured["HERMES_HOME"] == str(configured)


def test_guided_memory_home_ignores_ambient_home_override_without_mutation(
    monkeypatch, tmp_path,
):
    operator = _operator_home(monkeypatch, tmp_path)
    redirected = tmp_path / "redirected-home-SENSITIVE"
    redirected.mkdir()
    marker = redirected / "marker"
    marker.write_bytes(b"unchanged")
    monkeypatch.setenv("HOME", str(redirected))
    monkeypatch.delenv("MEMORYD_HOME", raising=False)
    monkeypatch.delenv("MEMORYD_DSN", raising=False)

    assert hermes.resolve_guided_memory_home() == operator.resolve() / "memory"
    assert marker.read_bytes() == b"unchanged"


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres:secret@remote.invalid:5432/memoryd",
        "postgresql://postgres:secret@127.0.0.1:5432/other",
        "postgresql://other:secret@127.0.0.1:5432/memoryd",
        "postgresql://postgres@127.0.0.1:5432/memoryd",
        "postgresql:///memoryd?host=/var/run/postgresql",
    ],
)
def test_managed_home_rejects_non_docker_dsn_without_echo(tmp_path, dsn):
    home = tmp_path / "memory"
    payload = _managed_payload(home)
    payload["dsn"] = dsn
    _safe_config(home / "config.json", payload)

    with pytest.raises(hermes.HermesInstallError) as caught:
        hermes.classify_memory_home(home)

    assert "dsn" in str(caught.value).lower()
    assert dsn not in str(caught.value)
    assert "secret" not in str(caught.value)


def _provider_http(
    monkeypatch: pytest.MonkeyPatch,
    *,
    chat_content: str = "ok",
    voyage_data: object = None,
    chat_raw: bytes | None = None,
    voyage_raw: bytes | None = None,
    chat_error: Exception | None = None,
    embedding_error: Exception | None = None,
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []

    class Response:
        def __init__(self, payload: object) -> None:
            self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.payload

    def urlopen(request, timeout):
        url = request.full_url
        event = {
            "url": url,
            "authorization": request.get_header("Authorization"),
            "body": json.loads(request.data),
            "timeout": timeout,
            "environment": {name: os.environ.get(name) for name in AFFECTED_ENV},
        }
        events.append(event)
        if url.endswith("/chat/completions"):
            if chat_error is not None:
                raise chat_error
            if chat_raw is not None:
                return Response(chat_raw)
            return Response({"choices": [{"message": {"content": chat_content}}]})
        if url == "https://api.voyageai.com/v1/embeddings":
            if embedding_error is not None:
                raise embedding_error
            if voyage_raw is not None:
                return Response(voyage_raw)
            data = voyage_data if voyage_data is not None else [{"embedding": [0.25, 0.75]}]
            return Response({"data": data})
        pytest.fail(f"unexpected provider URL: {url}")

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return events


def test_guided_environment_accepts_linux_ttys_and_read_only_systemd_probe(monkeypatch):
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(hermes.sys, "platform", "linux")
    monkeypatch.setattr(hermes.sys, "stdin", _tty())
    monkeypatch.setattr(hermes.sys, "stdout", _tty())
    monkeypatch.setattr(hermes.subprocess, "run", run)

    hermes.require_guided_environment()

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["systemctl", "--user", "is-system-running"]
    assert not ({"start", "stop", "restart", "enable", "disable"} & set(argv))
    assert kwargs.get("check") is False
    assert kwargs.get("stdout") is subprocess.DEVNULL
    assert kwargs.get("stderr") is subprocess.DEVNULL
    assert "capture_output" not in kwargs
    assert "text" not in kwargs


def test_guided_environment_rejects_non_linux_before_systemd(monkeypatch):
    monkeypatch.setattr(hermes.sys, "platform", "darwin")
    monkeypatch.setattr(hermes.subprocess, "run", lambda *a, **k: pytest.fail("systemctl must not run"))

    with pytest.raises(hermes.HermesInstallError, match="Linux"):
        hermes.require_guided_environment()


@pytest.mark.parametrize("non_tty", ["stdin", "stdout"])
def test_guided_environment_requires_both_interactive_streams(monkeypatch, non_tty):
    monkeypatch.setattr(hermes.sys, "platform", "linux")
    monkeypatch.setattr(hermes.sys, "stdin", _tty(non_tty != "stdin"))
    monkeypatch.setattr(hermes.sys, "stdout", _tty(non_tty != "stdout"))
    monkeypatch.setattr(hermes.subprocess, "run", lambda *a, **k: pytest.fail("systemctl must not run"))

    with pytest.raises(hermes.HermesInstallError, match="terminal|TTY"):
        hermes.require_guided_environment()


@pytest.mark.parametrize("failure", ["exit", "missing", "timeout"])
def test_guided_environment_rejects_unavailable_systemd_user_manager(monkeypatch, failure):
    monkeypatch.setattr(hermes.sys, "platform", "linux")
    monkeypatch.setattr(hermes.sys, "stdin", _tty())
    monkeypatch.setattr(hermes.sys, "stdout", _tty())

    calls: list[dict[str, object]] = []
    secret = "SYSTEMD-PROBE-SECRET-SENTINEL"

    def run(argv, **kwargs):
        calls.append(kwargs)
        if failure == "missing":
            raise OSError(secret)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 10, output=secret, stderr=secret)
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(hermes.subprocess, "run", run)

    with pytest.raises(hermes.HermesInstallError) as caught:
        hermes.require_guided_environment()

    assert "systemd" in str(caught.value).lower()
    assert calls[0].get("stdout") is subprocess.DEVNULL
    assert calls[0].get("stderr") is subprocess.DEVNULL
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = repr(caught.value) + "".join(traceback.format_exception(caught.value))
    assert secret not in rendered


def test_operator_confirmation_discloses_safety_consequences_without_secrets(monkeypatch, capsys):
    secret = "DO-NOT-ECHO-provider-secret"
    monkeypatch.setenv("OPENROUTER_API_KEY", secret)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "INSTALL")

    hermes.confirm_operator()

    output = capsys.readouterr().out.lower()
    assert "chat" in output and "tui" in output and "closed" in output
    assert "normal terminal" in output
    assert "gateway" in output and ("restart" in output or "stopped" in output)
    assert "target" in output and "after" in output and "confirmation" in output
    assert secret not in output


def test_operator_confirmation_requires_exact_install(monkeypatch):
    for response in ("install", " INSTALL", "INSTALL ", "", "yes"):
        monkeypatch.setattr(builtins, "input", lambda prompt="", value=response: value)
        with pytest.raises(hermes.HermesInstallError, match="confirm|cancel"):
            hermes.confirm_operator()


def test_nonexistent_home_is_fresh_and_remains_nonexistent(tmp_path):
    home = tmp_path / "memoryd"

    assert hermes.classify_memory_home(home) == "fresh"
    assert not home.exists()


def test_empty_owner_only_real_directory_is_fresh(tmp_path):
    home = tmp_path / "memoryd"
    home.mkdir()
    os.chmod(home, 0o700)

    assert hermes.classify_memory_home(home) == "fresh"
    assert list(home.iterdir()) == []
    assert home.stat().st_mode & 0o777 == 0o700


def test_empty_unsafe_symlink_and_special_home_shapes_are_rejected_without_mutation(tmp_path):
    bad_mode = tmp_path / "bad-mode"
    bad_mode.mkdir()
    os.chmod(bad_mode, 0o755)
    regular = tmp_path / "regular"
    regular.write_text("leave me", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)

    for home in (bad_mode, regular, link, fifo):
        before = home.lstat().st_mode
        with pytest.raises(hermes.HermesInstallError):
            hermes.classify_memory_home(home)
        assert home.lstat().st_mode == before

    assert regular.read_text(encoding="utf-8") == "leave me"
    assert link.is_symlink()


@pytest.mark.parametrize("include_env", [False, True])
def test_valid_managed_home_accepts_optional_env_object(tmp_path, include_env):
    home = tmp_path / ("with-env" if include_env else "without-env")
    config = _safe_config(home / "config.json", _managed_payload(home, include_env=include_env))
    before = config.read_bytes()

    assert hermes.classify_memory_home(home) == "managed"
    assert config.read_bytes() == before


def test_unknown_nonempty_home_is_rejected_without_mutation(tmp_path):
    home = tmp_path / "memoryd"
    home.mkdir()
    os.chmod(home, 0o700)
    marker = home / "operator-data.txt"
    marker.write_bytes(b"preserve exactly")
    before = (marker.read_bytes(), marker.stat().st_mode)

    with pytest.raises(hermes.HermesInstallError, match="unknown|unsafe|managed"):
        hermes.classify_memory_home(home)

    assert (marker.read_bytes(), marker.stat().st_mode) == before


def test_managed_home_rejects_non_regular_symlinked_or_unsafe_config(tmp_path):
    homes: list[Path] = []

    unsafe = tmp_path / "unsafe"
    unsafe_config = _safe_config(unsafe / "config.json", _managed_payload(unsafe))
    os.chmod(unsafe_config, 0o644)
    homes.append(unsafe)

    linked = tmp_path / "linked"
    linked.mkdir()
    os.chmod(linked, 0o700)
    real_config = _safe_config(tmp_path / "real-config.json", _managed_payload(linked))
    (linked / "config.json").symlink_to(real_config)
    homes.append(linked)

    directory = tmp_path / "directory-config"
    directory.mkdir()
    os.chmod(directory, 0o700)
    (directory / "config.json").mkdir()
    homes.append(directory)

    for home in homes:
        with pytest.raises(hermes.HermesInstallError):
            hermes.classify_memory_home(home)

    assert unsafe_config.read_text(encoding="utf-8")
    assert (linked / "config.json").is_symlink()
    assert (directory / "config.json").is_dir()


def test_managed_home_rejects_malformed_json_without_rewriting_it(tmp_path):
    home = tmp_path / "memoryd"
    config = _safe_config(home / "config.json", {})
    secret = "CONFIG-SECRET-SENTINEL"
    config.write_text(f'{{"env": {{"key": "{secret}"}}, definitely not json', encoding="utf-8")
    before = config.read_bytes()

    with pytest.raises(hermes.HermesInstallError, match="config|JSON") as caught:
        hermes.classify_memory_home(home)

    assert config.read_bytes() == before
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = repr(caught.value) + "".join(traceback.format_exception(caught.value))
    assert secret not in rendered


@pytest.mark.parametrize("parse_error", [OSError, ValueError, RecursionError])
def test_config_parser_failures_are_sanitized_without_exception_context(tmp_path, monkeypatch, parse_error):
    home = tmp_path / "memoryd"
    _safe_config(home / "config.json", _managed_payload(home))
    secret = "PARSER-CONFIG-SECRET-SENTINEL"

    def fail_parse(stream):
        raise parse_error(secret)

    monkeypatch.setattr(hermes.json, "load", fail_parse)

    with pytest.raises(hermes.HermesInstallError, match="config|JSON") as caught:
        hermes.classify_memory_home(home)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = repr(caught.value) + "".join(traceback.format_exception(caught.value))
    assert secret not in rendered


def test_config_decode_failure_is_sanitized_without_exception_context(tmp_path):
    home = tmp_path / "memoryd"
    config = _safe_config(home / "config.json", {})
    secret = "DECODE-CONFIG-SECRET-SENTINEL"
    config.write_bytes(b"\xff" + secret.encode())

    with pytest.raises(hermes.HermesInstallError, match="config|JSON") as caught:
        hermes.classify_memory_home(home)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = repr(caught.value) + "".join(traceback.format_exception(caught.value))
    assert secret not in rendered


@pytest.mark.parametrize("field", ["home", "port"])
def test_managed_home_rejects_mismatched_identity_fields(tmp_path, field):
    home = tmp_path / field
    payload = _managed_payload(home)
    payload[field] = str(tmp_path / "elsewhere") if field == "home" else 7438
    config = _safe_config(home / "config.json", payload)
    before = config.read_bytes()

    with pytest.raises(hermes.HermesInstallError, match=field):
        hermes.classify_memory_home(home)

    assert config.read_bytes() == before


@pytest.mark.parametrize(
    "patch",
    [
        {"dsn": ""},
        {"env": ["not", "an", "object"]},
    ],
)
def test_managed_home_rejects_invalid_required_schema(tmp_path, patch):
    home = tmp_path / "memoryd"
    payload = _managed_payload(home)
    payload.update(patch)
    _safe_config(home / "config.json", payload)

    with pytest.raises(hermes.HermesInstallError):
        hermes.classify_memory_home(home)


def test_credentials_use_nonempty_process_values_before_config_or_prompt(tmp_path, monkeypatch):
    config = _safe_config(tmp_path / "config.json", {"env": {
        "OPENROUTER_API_KEY": "UNSAFE-CONFIG-SECRET",
        "VOYAGE_API_KEY": "UNSAFE-CONFIG-SECRET",
    }})
    os.chmod(config, 0o644)
    monkeypatch.setenv("OPENROUTER_API_KEY", "process-openrouter")
    monkeypatch.setenv("VOYAGE_API_KEY", "process-voyage")
    monkeypatch.setattr(getpass, "getpass", lambda prompt: pytest.fail("must not inspect config or prompt"))

    credentials = hermes.collect_provider_credentials(config)

    assert credentials == hermes.ProviderCredentials("process-openrouter", "process-voyage")


def test_credentials_fall_through_empty_environment_to_safe_config(tmp_path, monkeypatch):
    config = _safe_config(tmp_path / "config.json", {"env": {
        "OPENROUTER_API_KEY": "config-openrouter",
        "VOYAGE_API_KEY": "config-voyage",
    }})
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setattr(getpass, "getpass", lambda prompt: pytest.fail("must not prompt"))

    credentials = hermes.collect_provider_credentials(config)

    assert credentials == hermes.ProviderCredentials("config-openrouter", "config-voyage")


def test_credentials_prompt_only_for_missing_values_without_echo(tmp_path, monkeypatch, capsys):
    config = _safe_config(tmp_path / "config.json", {"env": {"OPENROUTER_API_KEY": "config-openrouter"}})
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    prompts: list[str] = []

    def prompt(label: str) -> str:
        prompts.append(label)
        return "prompt-voyage"

    monkeypatch.setattr(getpass, "getpass", prompt)

    credentials = hermes.collect_provider_credentials(config)

    assert credentials == hermes.ProviderCredentials("config-openrouter", "prompt-voyage")
    assert len(prompts) == 1 and "voyage" in prompts[0].lower()
    output = capsys.readouterr()
    assert "prompt-voyage" not in output.out + output.err
    assert "config-openrouter" not in output.out + output.err


@pytest.mark.parametrize("empty_key", KEY_NAMES)
def test_credentials_reject_empty_prompt_results(tmp_path, monkeypatch, empty_key):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

    def prompt(label: str) -> str:
        is_target = ("openrouter" in label.lower()) == (empty_key == "OPENROUTER_API_KEY")
        return "" if is_target else "nonempty"

    monkeypatch.setattr(getpass, "getpass", prompt)

    with pytest.raises(hermes.HermesInstallError, match="credential|required"):
        hermes.collect_provider_credentials(tmp_path / "missing-config.json")


def test_credentials_never_read_secrets_from_non_owner_only_config(tmp_path, monkeypatch):
    secret = "UNSAFE-CONFIG-SECRET-SENTINEL"
    config = _safe_config(tmp_path / "config.json", {"env": {
        "OPENROUTER_API_KEY": secret,
        "VOYAGE_API_KEY": secret,
    }})
    os.chmod(config, 0o644)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setattr(getpass, "getpass", lambda prompt: pytest.fail("unsafe config must fail before prompting"))

    with pytest.raises(hermes.HermesInstallError) as caught:
        hermes.collect_provider_credentials(config)

    assert secret not in str(caught.value)


def test_validation_uses_canonical_provider_endpoints_minimally_and_restores_environment(monkeypatch):
    old_values = ("old-llm", "old-embed", "https://attacker.invalid/collect", "old-open", "old-voyage")
    for name, value in zip(AFFECTED_ENV, old_values):
        monkeypatch.setenv(name, value)
    before = dict(os.environ)
    events = _provider_http(monkeypatch)
    credentials = hermes.ProviderCredentials("new-open", "new-voyage")

    hermes.validate_provider_credentials(credentials)

    assert dict(os.environ) == before
    assert [event["url"] for event in events] == [
        "https://openrouter.ai/api/v1/chat/completions",
        "https://api.voyageai.com/v1/embeddings",
    ]
    expected_environment = {
        "MEMORYD_LLM": "openrouter",
        "MEMORYD_EMBED": "voyage",
        "MEMORYD_LLM_BASE": "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY": "new-open",
        "VOYAGE_API_KEY": "new-voyage",
    }
    assert all(event["environment"] == expected_environment for event in events)
    assert events[0]["authorization"] == "Bearer new-open"
    assert events[1]["authorization"] == "Bearer new-voyage"
    chat_body = events[0]["body"]
    embed_body = events[1]["body"]
    assert 0 < chat_body["max_tokens"] <= 8
    assert len(chat_body["messages"]) == 2
    assert embed_body["input"] == ["credential validation"]
    assert embed_body["output_dimension"] == 1024


def test_validation_redacts_keys_and_remote_body_and_restores_environment_on_chat_failure(monkeypatch, capsys):
    key = "MALICIOUS-KEY\nSHOULD-NOT-APPEAR"
    remote = "REMOTE-RESPONSE-BODY-SENTINEL"
    monkeypatch.setenv("MEMORYD_LLM", "previous")
    monkeypatch.delenv("MEMORYD_EMBED", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("VOYAGE_API_KEY", "previous-voyage")
    before = dict(os.environ)
    _provider_http(monkeypatch, chat_error=ValueError(remote))
    voyage_key = "voyage-secret"
    credentials = hermes.ProviderCredentials(key, voyage_key)

    with pytest.raises(hermes.HermesInstallError, match="chat|completion") as caught:
        hermes.validate_provider_credentials(credentials)

    assert dict(os.environ) == before
    rendered = (
        str(caught.value)
        + repr(caught.value)
        + "".join(traceback.format_exception(caught.value))
        + capsys.readouterr().out
    )
    assert key not in rendered and voyage_key not in rendered and remote not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_validation_wraps_embedding_failure_as_generic_stage_error(monkeypatch):
    remote = "EMBEDDING-REMOTE-BODY-SENTINEL"
    before = dict(os.environ)
    _provider_http(monkeypatch, embedding_error=RuntimeError(remote))
    openrouter_key = "open-secret"
    voyage_key = "voyage-secret"
    credentials = hermes.ProviderCredentials(openrouter_key, voyage_key)

    with pytest.raises(hermes.HermesInstallError, match="embed") as caught:
        hermes.validate_provider_credentials(credentials)

    assert dict(os.environ) == before
    rendered = repr(caught.value) + "".join(traceback.format_exception(caught.value))
    assert remote not in rendered
    assert openrouter_key not in rendered
    assert voyage_key not in rendered
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("stage", ["chat", "embed"])
def test_validation_sanitizes_malformed_provider_responses_without_exception_context(monkeypatch, stage):
    remote = "MALFORMED-PROVIDER-RESPONSE-SENTINEL"
    raw = f'{{"secret": "{remote}"'.encode()
    kwargs = {"chat_raw": raw} if stage == "chat" else {"voyage_raw": raw}
    _provider_http(monkeypatch, **kwargs)
    openrouter_key = "open-secret"
    voyage_key = "voyage-secret"
    credentials = hermes.ProviderCredentials(openrouter_key, voyage_key)
    error_pattern = "chat|completion" if stage == "chat" else "embed"

    with pytest.raises(hermes.HermesInstallError, match=error_pattern) as caught:
        hermes.validate_provider_credentials(credentials)

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    rendered = repr(caught.value) + "".join(traceback.format_exception(caught.value))
    assert remote not in rendered
    assert openrouter_key not in rendered
    assert voyage_key not in rendered


def test_validation_rejects_empty_completion(monkeypatch):
    _provider_http(monkeypatch, chat_content="")

    with pytest.raises(hermes.HermesInstallError, match="chat|completion"):
        hermes.validate_provider_credentials(hermes.ProviderCredentials("open", "voyage"))


@pytest.mark.parametrize(
    "voyage_data",
    [
        [],
        [{"embedding": [0.25]}, {"embedding": [0.75]}],
        [{"embedding": [float("nan")]}],
    ],
)
def test_validation_rejects_empty_multiple_or_nonfinite_voyage_results(monkeypatch, voyage_data):
    _provider_http(monkeypatch, voyage_data=voyage_data)

    with pytest.raises(hermes.HermesInstallError, match="embed"):
        hermes.validate_provider_credentials(hermes.ProviderCredentials("open", "voyage"))


@pytest.mark.parametrize(
    "invalid_embedding",
    [None, (), [], [0.25, 0.75], [[0.25], [0.75]], [[float("nan")]]],
)
def test_embedding_result_contract_rejects_flat_multiple_empty_or_nonfinite_values(invalid_embedding):
    assert not hermes._valid_embedding(invalid_embedding)


def test_embedding_result_contract_accepts_exactly_one_finite_nonempty_vector():
    assert hermes._valid_embedding([[0.25, 0.75]])


def test_failed_validation_never_mutates_target_filesystem(tmp_path, monkeypatch):
    home = tmp_path / "memoryd"
    home.mkdir()
    os.chmod(home, 0o700)
    marker = home / "operator-owned"
    marker.write_bytes(b"unchanged")
    before = (tuple(path.name for path in home.iterdir()), marker.read_bytes(), marker.stat().st_mode)
    _provider_http(monkeypatch, chat_error=ConnectionError("provider unavailable"))

    with pytest.raises(hermes.HermesInstallError):
        hermes.validate_provider_credentials(hermes.ProviderCredentials("open", "voyage"))

    after = (tuple(path.name for path in home.iterdir()), marker.read_bytes(), marker.stat().st_mode)
    assert after == before


def _hermes_target(tmp_path: Path) -> HermesTarget:
    root = tmp_path / "hermes"
    home = root / "profiles" / "work"
    home.mkdir(parents=True)
    os.chmod(home, 0o700)
    (root / "active_profile").write_text("work", encoding="utf-8")
    return HermesTarget(
        root=root.resolve(),
        home=home.resolve(),
        executable=tmp_path / "bin" / "hermes",
        python=tmp_path / "venv" / "bin" / "python",
    )


def _plugin_source(tmp_path: Path, version: str = "one") -> Path:
    source = tmp_path / "wheel-plugin"
    source.mkdir(exist_ok=True)
    (source / "__init__.py").write_text(f"VERSION = {version!r}\n", encoding="utf-8")
    (source / "plugin.yaml").write_text(
        f"name: memoryd\nversion: {version}\n", encoding="utf-8",
    )
    (source / "spool.py").write_text("QUEUE = True\n", encoding="utf-8")
    nested = source / "nested"
    nested.mkdir(exist_ok=True)
    (nested / "resource.txt").write_text(f"resource-{version}\n", encoding="utf-8")
    return source


def _file_manifest(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def test_guided_plugin_publication_is_exact_private_and_removes_stale_files(
    monkeypatch, tmp_path,
):
    target = _hermes_target(tmp_path)
    source = _plugin_source(tmp_path)
    monkeypatch.setattr(cli, "_resource_dir", lambda name: source)

    hermes.publish_guided_plugin(target)
    destination = target.home / "plugins" / "memoryd"
    assert _file_manifest(destination) == _file_manifest(source)

    (destination / "stale-injected.py").write_text("stale\n", encoding="utf-8")
    (destination / "__init__.py").write_text("tampered\n", encoding="utf-8")
    hermes.publish_guided_plugin(target)

    assert _file_manifest(destination) == _file_manifest(source)
    assert not (destination / "stale-injected.py").exists()
    assert json.loads((target.home / "memoryd.json").read_text(encoding="utf-8")) == {
        "url": "http://127.0.0.1:7437",
    }
    if os.name != "nt":
        assert stat.S_IMODE((target.home / "plugins").stat().st_mode) == 0o700
        assert stat.S_IMODE(destination.stat().st_mode) == 0o700
        assert all(
            stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
            for path in destination.rglob("*")
        )
        assert stat.S_IMODE((target.home / "memoryd.json").stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="guided mode is Linux-only")
@pytest.mark.parametrize("topology", ["plugins", "destination"])
def test_guided_plugin_publication_rejects_destination_symlink_topology(
    monkeypatch, tmp_path, topology,
):
    target = _hermes_target(tmp_path)
    source = _plugin_source(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    marker = external / "marker"
    marker.write_bytes(b"unchanged")
    plugins = target.home / "plugins"
    if topology == "plugins":
        plugins.symlink_to(external, target_is_directory=True)
    else:
        plugins.mkdir(mode=0o700)
        (plugins / "memoryd").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(cli, "_resource_dir", lambda name: source)

    with pytest.raises(hermes.HermesInstallError, match="plugin|symlink|topology"):
        hermes.publish_guided_plugin(target)

    assert marker.read_bytes() == b"unchanged"
    assert not list(target.home.glob(".memoryd*"))


@pytest.mark.skipif(os.name == "nt", reason="guided mode is Linux-only")
def test_guided_plugin_publication_rejects_source_symlink_and_preserves_prior(
    monkeypatch, tmp_path,
):
    target = _hermes_target(tmp_path)
    source = _plugin_source(tmp_path)
    monkeypatch.setattr(cli, "_resource_dir", lambda name: source)
    hermes.publish_guided_plugin(target)
    destination = target.home / "plugins" / "memoryd"
    before = _file_manifest(destination)
    external = tmp_path / "external-secret"
    external.write_bytes(b"must-not-copy")
    (source / "unsafe-link").symlink_to(external)

    with pytest.raises(hermes.HermesInstallError, match="source|symlink|plugin"):
        hermes.publish_guided_plugin(target)

    assert _file_manifest(destination) == before
    assert external.read_bytes() == b"must-not-copy"


def test_guided_plugin_interrupted_swap_restores_prior_without_partial_mix(
    monkeypatch, tmp_path,
):
    target = _hermes_target(tmp_path)
    source = _plugin_source(tmp_path, "one")
    monkeypatch.setattr(cli, "_resource_dir", lambda name: source)
    hermes.publish_guided_plugin(target)
    destination = target.home / "plugins" / "memoryd"
    before = _file_manifest(destination)
    (source / "__init__.py").write_text("VERSION = 'two'\n", encoding="utf-8")
    real_replace = hermes.os.replace

    def interrupt_stage(source_path, destination_path, *args, **kwargs):
        source_value = Path(source_path)
        destination_value = Path(destination_path)
        if (
            source_value.name.startswith(".memoryd-stage-")
            and destination_value.name == "memoryd"
        ):
            raise KeyboardInterrupt
        return real_replace(source_path, destination_path, *args, **kwargs)

    monkeypatch.setattr(hermes.os, "replace", interrupt_stage)

    with pytest.raises(KeyboardInterrupt):
        hermes.publish_guided_plugin(target)

    assert _file_manifest(destination) == before
    assert not list((target.home / "plugins").glob(".memoryd-*-*"))


def test_guided_plugin_staging_manifest_mismatch_preserves_prior(
    monkeypatch, tmp_path,
):
    target = _hermes_target(tmp_path)
    source = _plugin_source(tmp_path, "one")
    monkeypatch.setattr(cli, "_resource_dir", lambda name: source)
    hermes.publish_guided_plugin(target)
    destination = target.home / "plugins" / "memoryd"
    before = _file_manifest(destination)
    (source / "__init__.py").write_text("VERSION = 'two'\n", encoding="utf-8")
    real_copy = hermes._copy_plugin_tree_fd

    def tamper_after_copy(source_fd: int, stage_fd: int, prefix: Path = Path()):
        real_copy(source_fd, stage_fd, prefix)
        if prefix.parts:
            return
        descriptor = os.open(
            "__init__.py", os.O_WRONLY | os.O_TRUNC, dir_fd=stage_fd,
        )
        try:
            os.write(descriptor, b"tampered\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    monkeypatch.setattr(hermes, "_copy_plugin_tree_fd", tamper_after_copy)

    with pytest.raises(hermes.HermesInstallError, match="manifest|verify"):
        hermes.publish_guided_plugin(target)

    assert _file_manifest(destination) == before
    assert not list((target.home / "plugins").glob(".memoryd-*-*"))


def _publication_pair(target: HermesTarget) -> tuple[dict[str, bytes], bytes | None, int | None]:
    destination = target.home / "plugins" / "memoryd"
    config = target.home / "memoryd.json"
    return (
        _file_manifest(destination),
        config.read_bytes() if config.exists() else None,
        stat.S_IMODE(config.stat().st_mode) if config.exists() else None,
    )


def _publication_rerun(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, prior_config: bool = True,
) -> tuple[HermesTarget, Path, tuple[dict[str, bytes], bytes | None, int | None]]:
    target = _hermes_target(tmp_path)
    source = _plugin_source(tmp_path, "one")
    monkeypatch.setattr(cli, "_resource_dir", lambda name: source)
    hermes.publish_guided_plugin(target)
    config = target.home / "memoryd.json"
    if prior_config:
        config.write_bytes(b'{"url":"http://127.0.0.1:7444","prior":true}\n')
        os.chmod(config, 0o600)
    else:
        config.unlink()
    before = _publication_pair(target)
    (source / "__init__.py").write_text("VERSION = 'two'\n", encoding="utf-8")
    (source / "nested" / "resource.txt").write_text("resource-two\n", encoding="utf-8")
    return target, source, before


def _fd_target(descriptor: int) -> Path | None:
    try:
        return Path(os.readlink(f"/proc/self/fd/{descriptor}"))
    except OSError:
        return None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="fd publication is POSIX-only")
@pytest.mark.parametrize("prior_config", [False, True])
@pytest.mark.parametrize(
    "fault", ["before-config-replace", "config-replace", "config-mode", "home-fsync"],
)
def test_guided_plugin_precommit_fault_restores_exact_plugin_and_config_pair(
    monkeypatch, tmp_path, prior_config, fault,
):
    target, _source, before = _publication_rerun(
        monkeypatch, tmp_path, prior_config=prior_config,
    )
    real_replace = os.replace
    real_chmod = os.chmod
    real_fchmod = os.fchmod
    real_fsync = os.fsync
    fired = False

    def replace(source, destination, *args, **kwargs):
        nonlocal fired
        if (
            not fired
            and fault == "before-config-replace"
            and Path(destination).name == "memoryd.json"
        ):
            fired = True
            raise OSError("injected pre-config replace fault")
        result = real_replace(source, destination, *args, **kwargs)
        if fault == "config-replace" and Path(destination).name == "memoryd.json":
            fired = True
            raise OSError("injected config replace persistence fault")
        return result

    def chmod(path, mode, *args, **kwargs):
        nonlocal fired
        if fault == "config-mode" and "memoryd" in Path(path).name and Path(path).suffix in {".tmp", ".json"}:
            fired = True
            raise OSError("injected config mode fault")
        return real_chmod(path, mode, *args, **kwargs)

    def fchmod(descriptor, mode):
        nonlocal fired
        path = _fd_target(descriptor)
        result = real_fchmod(descriptor, mode)
        if fault == "config-mode" and path is not None and "config" in path.name:
            fired = True
            raise OSError("injected config mode fault")
        return result

    def fsync(descriptor):
        nonlocal fired
        result = real_fsync(descriptor)
        config = target.home / "memoryd.json"
        if (
            fault == "home-fsync"
            and _fd_target(descriptor) == target.home
            and config.exists()
            and config.read_bytes() != before[1]
        ):
            fired = True
            raise OSError("injected home fsync fault")
        return result

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "chmod", chmod)
    monkeypatch.setattr(os, "fchmod", fchmod)
    monkeypatch.setattr(os, "fsync", fsync)

    with pytest.raises(hermes.HermesInstallError):
        hermes.publish_guided_plugin(target)

    assert fired
    assert _publication_pair(target) == before


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="fd publication is POSIX-only")
@pytest.mark.parametrize("interruption", [KeyboardInterrupt(), SystemExit(143)])
def test_guided_plugin_interrupt_after_config_replace_restores_exact_pair(
    monkeypatch, tmp_path, interruption,
):
    target, _source, before = _publication_rerun(monkeypatch, tmp_path)
    real_replace = os.replace
    fired = False

    def replace(source, destination, *args, **kwargs):
        nonlocal fired
        result = real_replace(source, destination, *args, **kwargs)
        if not fired and Path(destination).name == "memoryd.json":
            fired = True
            raise interruption
        return result

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(type(interruption)):
        hermes.publish_guided_plugin(target)

    assert fired
    assert _publication_pair(target) == before


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="fd publication is POSIX-only")
def test_guided_plugin_rollback_deletion_interrupt_is_deferred_until_pair_restored(
    monkeypatch, tmp_path,
):
    target, _source, before = _publication_rerun(monkeypatch, tmp_path)
    real_replace = os.replace
    real_unlink = os.unlink
    replaced = False
    interrupted = False

    def replace(source, destination, *args, **kwargs):
        nonlocal replaced
        result = real_replace(source, destination, *args, **kwargs)
        if not replaced and Path(destination).name == "memoryd.json":
            replaced = True
            raise OSError("force rollback")
        return result

    def unlink(path, *args, **kwargs):
        nonlocal interrupted
        parent = _fd_target(kwargs.get("dir_fd", -1))
        if not interrupted and parent is not None and "discard" in parent.name:
            interrupted = True
            raise KeyboardInterrupt
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "unlink", unlink)
    with pytest.raises((KeyboardInterrupt, hermes.HermesInstallError)):
        hermes.publish_guided_plugin(target)

    assert replaced and interrupted
    assert _publication_pair(target) == before


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="fd publication is POSIX-only")
def test_guided_plugin_postcommit_final_fsync_interrupt_keeps_coherent_new_pair(
    monkeypatch, tmp_path,
):
    target, source, before = _publication_rerun(monkeypatch, tmp_path)
    real_fsync = os.fsync
    fired = False

    def fsync(descriptor):
        nonlocal fired
        result = real_fsync(descriptor)
        parent = _fd_target(descriptor)
        siblings = list((target.home / "plugins").glob(".memoryd-*-*"))
        config = target.home / "memoryd.json"
        if (
            not fired
            and parent in {target.home, target.home / "plugins"}
            and config.exists()
            and config.read_bytes() != before[1]
            and not siblings
        ):
            fired = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(KeyboardInterrupt):
        hermes.publish_guided_plugin(target)

    assert fired
    assert _file_manifest(target.home / "plugins" / "memoryd") == _file_manifest(source)
    assert json.loads((target.home / "memoryd.json").read_text(encoding="utf-8")) == {
        "url": "http://127.0.0.1:7437",
    }


def _real_sigint_publication_child(root: str) -> None:
    tmp_path = Path(root)
    target = _hermes_target(tmp_path)
    source = _plugin_source(tmp_path, "one")
    original_resource_dir = cli._resource_dir
    cli._resource_dir = lambda _name: source
    hermes.publish_guided_plugin(target)
    (source / "__init__.py").write_text("VERSION = 'two'\n", encoding="utf-8")
    lines, start_line = inspect.getsourcelines(hermes.publish_guided_plugin)
    cleanup_line = start_line + next(
        index for index, line in enumerate(lines)
        if "_unlink_config_at(home_fd, config_rollback)" in line
    )
    sent = False

    def trace(frame, event, _argument):
        nonlocal sent
        if (
            not sent
            and event == "line"
            and frame.f_code is hermes.publish_guided_plugin.__code__
            and frame.f_lineno == cleanup_line
        ):
            sent = True
            os.kill(os.getpid(), signal.SIGINT)
        return trace

    sys.settrace(trace)
    try:
        hermes.publish_guided_plugin(target)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("pending SIGINT was not delivered after cleanup")
    finally:
        sys.settrace(None)
        cli._resource_dir = original_resource_dir

    assert sent
    assert _file_manifest(target.home / "plugins" / "memoryd") == _file_manifest(source)
    assert json.loads((target.home / "memoryd.json").read_text(encoding="utf-8")) == {
        "url": "http://127.0.0.1:7437",
    }
    assert not list((target.home / "plugins").glob(".memoryd-*-*"))
    assert not list(target.home.glob(".memoryd-config-*-*"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="fd publication is POSIX-only")
def test_guided_plugin_real_sigint_during_postcommit_cleanup_is_deferred(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy; ns=runpy.run_path(" + repr(str(Path(__file__)))
            + "); ns['_real_sigint_publication_child'](" + repr(str(tmp_path)) + ")",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="fd publication is POSIX-only")
@pytest.mark.parametrize("race", ["source-root", "source-nested", "plugin-parent", "stage-nested"])
def test_guided_plugin_fd_races_do_not_touch_external_or_mix_prior_pair(
    monkeypatch, tmp_path, race,
):
    target, source, before = _publication_rerun(monkeypatch, tmp_path)
    external = tmp_path / f"external-{race}"
    external.mkdir(mode=0o700)
    marker = external / "marker"
    marker.write_bytes(b"external-unchanged")
    detached: Path | None = None
    real_open = os.open
    injected = False

    def open_file(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal injected, detached
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        name = Path(path).name if not isinstance(path, int) else ""
        parent = _fd_target(dir_fd) if dir_fd is not None else None
        opened = _fd_target(descriptor)
        if injected or not (flags & getattr(os, "O_DIRECTORY", 0)):
            return descriptor
        if race == "source-root" and Path(path) == source:
            detached = source.with_name("source-detached")
            source.rename(detached)
            source.mkdir(mode=0o700)
            _plugin_source(tmp_path, "attacker")
            injected = True
        elif race == "source-nested" and name == "nested" and parent == source:
            detached = source / "nested-detached"
            os.rename("nested", "nested-detached", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.symlink(external, "nested", target_is_directory=True, dir_fd=dir_fd)
            injected = True
        elif race == "plugin-parent" and name == "plugins" and parent == target.home:
            detached = target.home / "plugins-detached"
            os.rename("plugins", "plugins-detached", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.symlink(external, "plugins", target_is_directory=True, dir_fd=dir_fd)
            injected = True
        elif race == "stage-nested" and name == "nested" and parent is not None and ".memoryd-stage-" in parent.name:
            detached = parent / "nested-detached"
            os.rename("nested", "nested-detached", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            os.symlink(external, "nested", target_is_directory=True, dir_fd=dir_fd)
            injected = True
        return descriptor

    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(hermes.HermesInstallError):
        hermes.publish_guided_plugin(target)

    assert injected
    assert marker.read_bytes() == b"external-unchanged"
    assert list(external.iterdir()) == [marker]
    assert (target.home / "memoryd.json").read_bytes() == before[1]
    if race == "plugin-parent":
        assert detached is not None
        assert _file_manifest(detached / "memoryd") == before[0]
    else:
        assert _publication_pair(target) == before


def _backup_row(path: Path, *, ok: bool = True) -> backup.BackupListing:
    return backup.BackupListing(
        timestamp=path.name.removesuffix("-v1"),
        path=path,
        ok=ok,
        reason="ok" if ok else "corrupt",
    )


def _prepare_core(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> tuple[HermesTarget, Path, hermes.ProviderCredentials]:
    target = _hermes_target(tmp_path)
    memory_home = tmp_path / "memory"
    monkeypatch.setenv("HERMES_HOME", str(target.root))
    monkeypatch.setattr(cli, "_home", lambda: memory_home)
    monkeypatch.setattr(
        hermes, "resolve_guided_memory_home", lambda: memory_home, raising=False,
    )
    monkeypatch.delenv("MEMORYD_DSN", raising=False)
    credentials = hermes.ProviderCredentials("new-openrouter", "new-voyage")
    return target, memory_home, credentials


def test_core_install_orders_revalidation_install_backup_verification_and_restart_health(
    monkeypatch, tmp_path,
):
    target, memory_home, credentials = _prepare_core(monkeypatch, tmp_path)
    old = tmp_path / "backups" / "20260712T010203Z-v1"
    new = tmp_path / "backups" / "20260713T010203Z-v1"
    events: list[str] = []
    listings = iter(([_backup_row(old)], [_backup_row(old), _backup_row(new)]))

    def resolve_home():
        events.append("profile-revalidation")
        return target.root, target.home

    def classify(home):
        events.append("memory-home-revalidation")
        assert home == memory_home
        return "fresh"

    def install(options):
        events.append("core-install")
        assert options.hermes_home == target.home
        assert options.publish_hermes_plugin is False
        assert {name: os.environ[name] for name in INSTALL_ENV} == {
            "HERMES_HOME": str(target.home),
            "MEMORYD_HOME": str(memory_home),
            "OPENROUTER_API_KEY": credentials.openrouter_key,
            "VOYAGE_API_KEY": credentials.voyage_key,
            "MEMORYD_LLM": "openrouter",
            "MEMORYD_EMBED": "voyage",
        }
        return 0

    def publish(actual_target):
        assert actual_target == target
        events.append("plugin-publication")

    def list_backups():
        events.append("list-before" if "backup-service" not in events else "list-after")
        return next(listings)

    def run(command, timeout=120):
        events.append("backup-service")
        assert command == [
            "systemctl", "--user", "start", "--wait",
            "memoryd-backup-initial.service"]
        assert timeout >= 600
        return 0, ""

    monkeypatch.setattr(
        hermes, "resolve_guided_hermes_home", resolve_home, raising=False,
    )
    monkeypatch.setattr(hermes, "classify_memory_home", classify)
    monkeypatch.setattr(cli, "install", install)
    monkeypatch.setattr(hermes, "publish_guided_plugin", publish, raising=False)
    monkeypatch.setattr(backup, "list_backups", list_backups)
    monkeypatch.setattr(cli, "_run", run)
    monkeypatch.setattr(
        backup, "verify_snapshot",
        lambda path: events.append("verify-new") or backup.Verification(path == new))
    monkeypatch.setattr(
        cli, "_wait_for_healthy_daemon",
        lambda: events.append("restart-health") or True,
        raising=False,
    )

    assert hermes.install_hermes_core(target, credentials) == new
    assert events == [
        "profile-revalidation",
        "memory-home-revalidation",
        "core-install",
        "plugin-publication",
        "list-before",
        "backup-service",
        "list-after",
        "verify-new",
        "restart-health",
    ]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="guided mode is Linux-only")
def test_core_install_pins_home_for_real_autostart_and_default_backup_selection(
    monkeypatch, tmp_path,
):
    target = _hermes_target(tmp_path)
    operator = _operator_home(monkeypatch, tmp_path).resolve()
    memory_home = operator / "memory"
    hostile = tmp_path / "hostile-home"
    hostile.mkdir(mode=0o700)
    marker = hostile / "marker"
    marker.write_bytes(b"untouched")
    monkeypatch.setenv("HOME", str(hostile))
    monkeypatch.setenv("HERMES_HOME", str(target.root))
    monkeypatch.delenv("MEMORYD_HOME", raising=False)
    monkeypatch.delenv("MEMORYD_DSN", raising=False)
    monkeypatch.setattr(
        hermes, "resolve_guided_hermes_home", lambda: (target.root, target.home),
    )
    monkeypatch.setattr(hermes, "classify_memory_home", lambda home: "fresh")
    monkeypatch.setattr(hermes, "publish_guided_plugin", lambda _target: None)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli, "_wait_for_healthy_daemon", lambda: True)
    monkeypatch.setattr(backup, "verify_snapshot", lambda _path: backup.Verification(True))
    calls: list[list[str]] = []

    def run(command, timeout=120):
        calls.append(list(command))
        if command[-1] == "memoryd-backup-initial.service":
            snapshot = memory_home / "backups" / "20260714T010203Z-v1"
            snapshot.mkdir(parents=True)
        return 0, ""

    def install(options):
        assert os.environ["HOME"] == str(operator)
        assert cli._home() == memory_home
        cli.install_autostart(_hermes_mode=True)
        return 0

    monkeypatch.setattr(cli, "_run", run)
    monkeypatch.setattr(cli, "install", install)

    snapshot = hermes.install_hermes_core(
        target, hermes.ProviderCredentials("openrouter", "voyage"),
    )

    assert snapshot == memory_home / "backups" / "20260714T010203Z-v1"
    assert (operator / ".config/systemd/user/memoryd.service").is_file()
    assert not (hostile / ".config").exists()
    assert not (hostile / "memory").exists()
    assert marker.read_bytes() == b"untouched"
    assert os.environ["HOME"] == str(hostile)


def test_core_install_uses_explicit_profile_skips_hooks_persists_providers_and_reruns(
    monkeypatch, tmp_path,
):
    target, memory_home, credentials = _prepare_core(monkeypatch, tmp_path)
    plugin_source = tmp_path / "canonical-plugin"
    plugin_source.mkdir()
    (plugin_source / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
    (plugin_source / "plugin.yaml").write_text("name: memoryd\n", encoding="utf-8")
    (plugin_source / "spool.py").write_text("QUEUE = True\n", encoding="utf-8")
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_base.sql").write_text("SELECT 1;\n", encoding="utf-8")
    _safe_config(memory_home / "config.json", _managed_payload(memory_home))
    monkeypatch.setenv("OPENROUTER_API_KEY", "caller-openrouter")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("MEMORYD_LLM", "caller-llm")
    monkeypatch.delenv("MEMORYD_EMBED", raising=False)
    before = {name: os.environ.get(name) for name in INSTALL_ENV}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, *_args):
            return None

    monkeypatch.setitem(
        sys.modules, "psycopg",
        SimpleNamespace(connect=lambda *_args, **_kwargs: Connection()))
    monkeypatch.setattr(cli, "ensure_container", lambda: pytest.fail("Docker must not run"))
    monkeypatch.setattr(cli, "apply_migrations", lambda _dsn: [])
    monkeypatch.setattr(
        cli, "_resource_dir",
        lambda name: plugin_source if name == "hermes_plugin" else migrations)
    monkeypatch.setattr(
        cli, "register_claude_hooks",
        lambda: pytest.fail("Claude hooks must be skipped in Hermes mode"))
    autostart_modes: list[bool] = []
    monkeypatch.setattr(
        cli, "install_autostart",
        lambda *, _hermes_mode=False: autostart_modes.append(_hermes_mode))
    monkeypatch.setattr(cli, "_start_daemon_now", lambda: None)
    monkeypatch.setattr(cli, "_wait_for_healthy_daemon", lambda: True, raising=False)
    monkeypatch.setattr(cli, "status", lambda: 0)

    snapshots: list[backup.BackupListing] = []
    generated = iter(("20260713T010203Z-v1", "20260713T020304Z-v1"))

    def run_backup(command, timeout=120):
        assert command[-1] == "memoryd-backup-initial.service"
        path = tmp_path / "backups" / next(generated)
        snapshots.append(_backup_row(path))
        return 0, ""

    monkeypatch.setattr(cli, "_run", run_backup)
    monkeypatch.setattr(backup, "list_backups", lambda: list(snapshots))
    monkeypatch.setattr(backup, "verify_snapshot", lambda _path: backup.Verification(True))

    first = hermes.install_hermes_core(target, credentials)
    installed = target.home / "plugins" / "memoryd"
    assert first == snapshots[0].path
    assert (installed / "__init__.py").read_text(encoding="utf-8") == "VERSION = 1\n"
    hermes_config = target.home / "memoryd.json"
    assert json.loads(hermes_config.read_text(encoding="utf-8")) == {
        "url": "http://127.0.0.1:7437"}
    config = json.loads((memory_home / "config.json").read_text(encoding="utf-8"))
    assert config["env"] == {
        "OPENROUTER_API_KEY": credentials.openrouter_key,
        "VOYAGE_API_KEY": credentials.voyage_key,
        "MEMORYD_LLM": "openrouter",
        "MEMORYD_EMBED": "voyage",
    }
    if os.name != "nt":
        assert stat.S_IMODE(hermes_config.stat().st_mode) == 0o600
        assert stat.S_IMODE((memory_home / "config.json").stat().st_mode) == 0o600
    assert {name: os.environ.get(name) for name in INSTALL_ENV} == before

    (plugin_source / "__init__.py").write_text("VERSION = 2\n", encoding="utf-8")
    second = hermes.install_hermes_core(target, credentials)
    assert second == snapshots[1].path
    assert (installed / "__init__.py").read_text(encoding="utf-8") == "VERSION = 2\n"
    assert hermes.classify_memory_home(memory_home) == "managed"
    assert {name: os.environ.get(name) for name in INSTALL_ENV} == before
    assert autostart_modes == [True, True]


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("MIGRATION-SECRET-SENTINEL"), SystemExit("CORE-SECRET-SENTINEL")],
)
def test_core_install_sanitizes_core_or_migration_failure_and_restores_environment(
    monkeypatch, tmp_path, failure,
):
    target, _memory_home, credentials = _prepare_core(monkeypatch, tmp_path)
    for name, value in zip(
        INSTALL_ENV,
        (
            str(target.root), str(tmp_path / "old-memory"), "old-open",
            "old-voyage", "old-llm", "old-embed",
        ),
    ):
        monkeypatch.setenv(name, value)
    before = dict(os.environ)
    monkeypatch.setattr(cli, "install", lambda _options: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(
        backup, "list_backups", lambda: pytest.fail("backup must not start"))

    with pytest.raises(hermes.HermesInstallError, match="core|install") as caught:
        hermes.install_hermes_core(target, credentials)

    assert dict(os.environ) == before
    rendered = repr(caught.value) + "".join(traceback.format_exception(caught.value))
    assert "SECRET-SENTINEL" not in rendered


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MEMORYD_HOME", "/tmp/late-home-SENSITIVE"),
        (
            "MEMORYD_DSN",
            "postgresql://postgres:SENSITIVE@remote.invalid/unrelated",
        ),
    ],
)
def test_core_revalidation_rejects_late_memory_redirect_before_mutation(
    monkeypatch, tmp_path, name, value,
):
    target = _hermes_target(tmp_path)
    operator = _operator_home(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(target.root))
    monkeypatch.delenv("MEMORYD_HOME", raising=False)
    monkeypatch.delenv("MEMORYD_DSN", raising=False)
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        cli, "install", lambda _options: pytest.fail("target mutation must not start"),
    )

    with pytest.raises(hermes.HermesInstallError) as caught:
        hermes.install_hermes_core(
            target, hermes.ProviderCredentials("openrouter", "voyage"),
        )

    assert operator.exists()
    assert value not in str(caught.value)
    assert "SENSITIVE" not in str(caught.value)


def test_managed_rerun_adopts_config_dsn_before_migration_failure_and_preserves_evidence(
    monkeypatch, tmp_path,
):
    target, memory_home, credentials = _prepare_core(monkeypatch, tmp_path)
    dsn = "postgresql://postgres:password@127.0.0.1:5432/memoryd"
    payload = _managed_payload(memory_home)
    payload["dsn"] = dsn
    config = _safe_config(memory_home / "config.json", payload)
    evidence = {
        config: config.read_bytes(),
        memory_home / "archive" / "memory.jsonl": b"archive evidence",
        memory_home / "spool" / "incoming" / "job.json": b"spool evidence",
        memory_home / "backups" / "existing" / "manifest.json": b"backup evidence",
        tmp_path / "database-volume.marker": b"database evidence",
    }
    for path, contents in evidence.items():
        if path == config:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    before = {path: path.read_bytes() for path in evidence}
    monkeypatch.delenv("MEMORYD_DSN", raising=False)
    connected: list[str] = []
    migrated: list[str] = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, *_args):
            return None

    monkeypatch.setitem(
        sys.modules, "psycopg",
        SimpleNamespace(connect=lambda value, **_kwargs: (
            connected.append(value) or Connection())))
    monkeypatch.setattr(
        cli, "ensure_container", lambda: pytest.fail("managed DSN must skip Docker"))

    def fail_migrations(value: str):
        migrated.append(value)
        raise RuntimeError("MIGRATION-SECRET-SENTINEL")

    monkeypatch.setattr(cli, "apply_migrations", fail_migrations)
    monkeypatch.setattr(
        backup, "list_backups", lambda: pytest.fail("backup must not start"))

    with pytest.raises(hermes.HermesInstallError, match="core|install") as caught:
        hermes.install_hermes_core(target, credentials)

    assert connected == [dsn]
    assert migrated == [dsn]
    assert "MIGRATION-SECRET-SENTINEL" not in str(caught.value)
    assert {path: path.read_bytes() for path in evidence} == before


def test_core_install_requires_successful_systemd_backup_and_preserves_evidence(
    monkeypatch, tmp_path,
):
    target, memory_home, credentials = _prepare_core(monkeypatch, tmp_path)
    _safe_config(memory_home / "config.json", _managed_payload(memory_home))
    evidence = {
        memory_home / "archive" / "memory.jsonl": b"archive evidence",
        memory_home / "spool" / "incoming" / "job.json": b"spool evidence",
        memory_home / "logs" / "daemon.log": b"log evidence",
        memory_home / "backups" / "20260712T010203Z-v1" / "manifest.json": b"backup evidence",
        tmp_path / "database-volume.marker": b"database evidence",
    }
    stale_plugin = target.home / "plugins" / "memoryd" / "operator-note"
    stale_plugin.parent.mkdir(parents=True, exist_ok=True)
    stale_plugin.write_bytes(b"stale plugin injection")
    for path, payload in evidence.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    before = {path: path.read_bytes() for path in evidence}
    old = _backup_row(memory_home / "backups" / "20260712T010203Z-v1")
    monkeypatch.setattr(cli, "install", lambda _options: 0)
    monkeypatch.setattr(backup, "list_backups", lambda: [old])
    monkeypatch.setattr(cli, "_run", lambda _command, timeout=120: (1, "SECRET service detail"))

    with pytest.raises(hermes.HermesInstallError, match="backup|service") as caught:
        hermes.install_hermes_core(target, credentials)

    assert "SECRET service detail" not in str(caught.value)
    assert {path: path.read_bytes() for path in evidence} == before
    assert not stale_plugin.exists()


@pytest.mark.parametrize("new_count", [0, 2])
def test_core_install_requires_exactly_one_new_generated_snapshot(
    monkeypatch, tmp_path, new_count,
):
    target, _memory_home, credentials = _prepare_core(monkeypatch, tmp_path)
    old = _backup_row(tmp_path / "backups" / "20260712T010203Z-v1")
    additions = [
        _backup_row(tmp_path / "backups" / f"20260713T0{index}0203Z-v1")
        for index in range(new_count)
    ]
    listings = iter(([old], [old, *additions]))
    monkeypatch.setattr(cli, "install", lambda _options: 0)
    monkeypatch.setattr(backup, "list_backups", lambda: next(listings))
    monkeypatch.setattr(cli, "_run", lambda _command, timeout=120: (0, ""))
    monkeypatch.setattr(
        backup, "verify_snapshot",
        lambda _path: pytest.fail("ambiguous snapshot must not be verified"))

    with pytest.raises(hermes.HermesInstallError, match="exactly one|snapshot"):
        hermes.install_hermes_core(target, credentials)


def test_core_initial_backup_preserves_more_than_scheduled_retention_limit(
    monkeypatch, tmp_path,
):
    target, _memory_home, credentials = _prepare_core(monkeypatch, tmp_path)
    before_paths = {
        tmp_path / "backups" / f"202607{day:02d}T010203Z-v1"
        for day in range(1, 16)
    }
    created = tmp_path / "backups" / "20260716T010203Z-v1"
    after_paths = before_paths | {created}
    listings = iter((
        [_backup_row(path) for path in sorted(before_paths)],
        [_backup_row(path) for path in sorted(after_paths)],
    ))
    commands: list[list[str]] = []
    monkeypatch.setattr(cli, "install", lambda _options: 0)
    monkeypatch.setattr(backup, "list_backups", lambda: next(listings))
    monkeypatch.setattr(cli, "_run", lambda command, timeout=120: (
        commands.append(command) or (0, "")))
    monkeypatch.setattr(
        backup, "verify_snapshot", lambda _path: backup.Verification(True))
    monkeypatch.setattr(cli, "_wait_for_healthy_daemon", lambda: True, raising=False)

    assert hermes.install_hermes_core(target, credentials) == created
    assert before_paths <= after_paths
    assert after_paths - before_paths == {created}
    assert commands == [[
        "systemctl", "--user", "start", "--wait",
        "memoryd-backup-initial.service"]]


def test_core_rejects_initial_backup_that_prunes_existing_snapshot(
    monkeypatch, tmp_path,
):
    target, _memory_home, credentials = _prepare_core(monkeypatch, tmp_path)
    before_paths = [
        tmp_path / "backups" / f"202607{day:02d}T010203Z-v1"
        for day in range(1, 16)
    ]
    created = tmp_path / "backups" / "20260716T010203Z-v1"
    listings = iter((
        [_backup_row(path) for path in before_paths],
        [_backup_row(path) for path in [*before_paths[1:], created]],
    ))
    monkeypatch.setattr(cli, "install", lambda _options: 0)
    monkeypatch.setattr(backup, "list_backups", lambda: next(listings))
    monkeypatch.setattr(cli, "_run", lambda _command, timeout=120: (0, ""))
    monkeypatch.setattr(
        backup, "verify_snapshot",
        lambda _path: pytest.fail("pruning must fail before new verification"))

    with pytest.raises(hermes.HermesInstallError, match="preserv|disappear|backup"):
        hermes.install_hermes_core(target, credentials)


def test_core_install_rejects_failed_snapshot_verification(
    monkeypatch, tmp_path,
):
    target, _memory_home, credentials = _prepare_core(monkeypatch, tmp_path)
    snapshot = tmp_path / "backups" / "20260713T010203Z-v1"
    listings = iter(([], [_backup_row(snapshot, ok=False)]))
    monkeypatch.setattr(cli, "install", lambda _options: 0)
    monkeypatch.setattr(backup, "list_backups", lambda: next(listings))
    monkeypatch.setattr(cli, "_run", lambda _command, timeout=120: (0, ""))
    monkeypatch.setattr(
        backup, "verify_snapshot",
        lambda _path: backup.Verification(False, "SECRET corrupt detail"))
    monkeypatch.setattr(
        cli, "_wait_for_healthy_daemon",
        lambda: pytest.fail("unverified backup must not reach health check"),
        raising=False,
    )

    with pytest.raises(hermes.HermesInstallError, match="verification|backup") as caught:
        hermes.install_hermes_core(target, credentials)

    assert "SECRET corrupt detail" not in str(caught.value)


def test_core_install_requires_daemon_restart_health_after_verified_backup(
    monkeypatch, tmp_path,
):
    target, _memory_home, credentials = _prepare_core(monkeypatch, tmp_path)
    snapshot = tmp_path / "backups" / "20260713T010203Z-v1"
    listings = iter(([], [_backup_row(snapshot)]))
    monkeypatch.setattr(cli, "install", lambda _options: 0)
    monkeypatch.setattr(backup, "list_backups", lambda: next(listings))
    monkeypatch.setattr(cli, "_run", lambda _command, timeout=120: (0, ""))
    monkeypatch.setattr(backup, "verify_snapshot", lambda _path: backup.Verification(True))
    monkeypatch.setattr(cli, "_wait_for_healthy_daemon", lambda: False, raising=False)

    with pytest.raises(hermes.HermesInstallError, match="healthy|restart"):
        hermes.install_hermes_core(target, credentials)


@pytest.mark.parametrize("change", ["identity", "mode"])
def test_core_install_revalidates_authoritative_profile_before_first_mutation(
    monkeypatch, tmp_path, change,
):
    target, _memory_home, credentials = _prepare_core(monkeypatch, tmp_path)
    if change == "identity":
        other = target.root / "profiles" / "other"
        other.mkdir()
        os.chmod(other, 0o700)
        (target.root / "active_profile").write_text("other", encoding="utf-8")
    else:
        os.chmod(target.home, 0o755)
    monkeypatch.setattr(
        cli, "install", lambda _options: pytest.fail("mutation started before revalidation"))

    with pytest.raises(hermes.HermesInstallError, match="revalid|profile|target"):
        hermes.install_hermes_core(target, credentials)
