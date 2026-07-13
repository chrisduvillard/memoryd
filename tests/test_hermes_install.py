from __future__ import annotations

import builtins
import getpass
import json
import os
import subprocess
import traceback
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import memoryd.hermes_install as hermes


KEY_NAMES = ("OPENROUTER_API_KEY", "VOYAGE_API_KEY")
AFFECTED_ENV = ("MEMORYD_LLM", "MEMORYD_EMBED", "MEMORYD_LLM_BASE", *KEY_NAMES)


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
        "dsn": "postgresql://memoryd@localhost/memoryd",
        "port": 7437,
        "home": str(home.resolve()),
    }
    if include_env:
        payload["env"] = {
            "OPENROUTER_API_KEY": "config-openrouter",
            "VOYAGE_API_KEY": "config-voyage",
        }
    return payload


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
        return subprocess.CompletedProcess(argv, 0, stdout="running\n", stderr="")

    monkeypatch.setattr(hermes.sys, "platform", "linux")
    monkeypatch.setattr(hermes.sys, "stdin", _tty())
    monkeypatch.setattr(hermes.sys, "stdout", _tty())
    monkeypatch.setattr(hermes.subprocess, "run", run)

    hermes.require_guided_environment()

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:2] == ["systemctl", "--user"]
    assert not ({"start", "stop", "restart", "enable", "disable"} & set(argv))
    assert kwargs.get("check") is False


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


@pytest.mark.parametrize("failure", ["exit", "missing"])
def test_guided_environment_rejects_unavailable_systemd_user_manager(monkeypatch, failure):
    monkeypatch.setattr(hermes.sys, "platform", "linux")
    monkeypatch.setattr(hermes.sys, "stdin", _tty())
    monkeypatch.setattr(hermes.sys, "stdout", _tty())

    def run(argv, **kwargs):
        if failure == "missing":
            raise OSError("systemctl unavailable: remote-body-sentinel")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="remote-body-sentinel")

    monkeypatch.setattr(hermes.subprocess, "run", run)

    with pytest.raises(hermes.HermesInstallError) as caught:
        hermes.require_guided_environment()

    assert "systemd" in str(caught.value).lower()
    assert "remote-body-sentinel" not in str(caught.value)


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
