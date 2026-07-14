from __future__ import annotations

import sys
from pathlib import Path

import pytest

from memoryd import cli


INSTALL_USAGE = "usage: memoryd install [--hermes]\n"


def _run_main(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> int:
    monkeypatch.setattr(sys, "argv", ["memoryd", "install", *arguments])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    return int(caught.value.code)


def test_install_without_arguments_uses_existing_generic_installer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "install", lambda: calls.append("generic") or 0)

    assert _run_main(monkeypatch) == 0
    assert calls == ["generic"]


@pytest.mark.parametrize("exit_code", [0, 1, 130, 143])
def test_exact_hermes_flag_lazily_dispatches_and_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch, exit_code: int,
) -> None:
    import memoryd.hermes_install as hermes_install

    calls: list[str] = []
    monkeypatch.setattr(
        hermes_install,
        "guided_hermes_install",
        lambda: calls.append("guided") or exit_code,
    )
    monkeypatch.setattr(
        cli, "install", lambda: pytest.fail("generic installer must not run")
    )

    assert _run_main(monkeypatch, "--hermes") == exit_code
    assert calls == ["guided"]


@pytest.mark.parametrize(
    "arguments",
    [
        ("--help",),
        ("--hermes", "--hermes"),
        ("--hermes", "extra"),
        ("extra",),
        ("--openrouter-api-key=do-not-echo",),
        ("--voyage-api-key", "do-not-echo"),
    ],
)
def test_other_install_arguments_are_rejected_without_echo_or_installation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: tuple[str, ...],
) -> None:
    import memoryd.hermes_install as hermes_install

    monkeypatch.setattr(
        cli, "install", lambda: pytest.fail("generic installer must not run")
    )
    monkeypatch.setattr(
        hermes_install,
        "guided_hermes_install",
        lambda: pytest.fail("guided installer must not run"),
    )

    assert _run_main(monkeypatch, *arguments) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == INSTALL_USAGE
    assert "do-not-echo" not in output.err


def test_top_level_usage_advertises_guided_hermes_install() -> None:
    assert "memoryd install --hermes" in cli.USAGE


def test_guided_plugin_copy_suppresses_manual_activation_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "plugin.yaml").write_text("name: memoryd\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_resource_dir", lambda _name: plugin)

    cli.install_hermes_plugin(profile, show_activation_hint=False)

    assert "hermes config set" not in capsys.readouterr().out
