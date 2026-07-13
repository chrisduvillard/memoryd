#!/usr/bin/env python3
"""Check memoryd against the pinned Hermes Agent MemoryProvider contract."""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import sys
import types
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
PINNED_CONTRACT = REPO / "scripts" / "_stubs" / "agent" / "memory_provider.py"
PLUGIN_PATH = REPO / "hermes_plugin" / "memoryd" / "__init__.py"
PINNED_TAG = "v2026.6.5"
PINNED_COMMIT = "3c231eb3979ab9c57d5cd6d02f1d577a3b718b43"
REQUIRED_METHODS = (
    "is_available",
    "initialize",
    "system_prompt_block",
    "prefetch",
    "queue_prefetch",
    "sync_turn",
    "get_tool_schemas",
    "handle_tool_call",
    "shutdown",
    "get_config_schema",
    "save_config",
)


def _load_file(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_source_contract(source_root: Path):
    provider_path = source_root.resolve() / "agent" / "memory_provider.py"
    if not provider_path.is_file():
        raise FileNotFoundError(
            f"Hermes source root has no agent/memory_provider.py: {source_root}"
        )
    agent_package = types.ModuleType("agent")
    agent_package.__path__ = [str(provider_path.parent)]
    sys.modules["agent"] = agent_package
    return _load_file("agent.memory_provider", provider_path), provider_path


def _load_installed_contract():
    module = importlib.import_module("agent.memory_provider")
    origin = Path(module.__file__).resolve() if module.__file__ else Path("<unknown>")
    return module, origin


def _member_signature(member: Any) -> str | None:
    if isinstance(member, property):
        member = member.fget
    if not callable(member):
        return None
    return str(inspect.signature(member, eval_str=False))


def _public_contract(provider_class: type) -> dict[str, tuple[str, bool]]:
    contract: dict[str, tuple[str, bool]] = {}
    for name, member in vars(provider_class).items():
        if name.startswith("_"):
            continue
        signature = _member_signature(member)
        if signature is None:
            continue
        contract[name] = (signature, bool(getattr(member, "__isabstractmethod__", False)))
    return contract


def _compare_contracts(pinned_class: type, checked_class: type) -> list[str]:
    errors: list[str] = []
    pinned = _public_contract(pinned_class)
    checked = _public_contract(checked_class)
    for name, (pinned_signature, pinned_abstract) in pinned.items():
        if name not in checked:
            errors.append(f"contract removed public method/property {name}")
            continue
        checked_signature, checked_abstract = checked[name]
        if checked_signature != pinned_signature:
            errors.append(
                f"contract signature changed for {name}: pinned {pinned_signature}; "
                f"checked {checked_signature}"
            )
        if checked_abstract != pinned_abstract:
            errors.append(
                f"contract abstract status changed for {name}: "
                f"pinned={pinned_abstract}, checked={checked_abstract}"
            )
    pinned_abstracts = set(pinned_class.__abstractmethods__)
    checked_abstracts = set(checked_class.__abstractmethods__)
    for name in sorted(checked_abstracts - pinned_abstracts):
        errors.append(f"contract added abstract method/property {name}")
    for name in sorted(pinned_abstracts - checked_abstracts):
        errors.append(f"contract removed pinned abstract requirement {name}")
    return errors


def _validate_plugin(checked_class: type, plugin_path: Path) -> list[str]:
    errors: list[str] = []
    daemon_before = sys.modules.get("memoryd")
    module_name = "_memoryd_hermes_contract_plugin"
    sys.modules.pop(module_name, None)
    plugin = None
    import_error = None
    try:
        plugin = _load_file(module_name, plugin_path.resolve())
    except Exception as exc:
        import_error = exc
    finally:
        if sys.modules.get("memoryd") is not daemon_before:
            errors.append("checker imported or replaced the memoryd daemon package")
            if daemon_before is None:
                sys.modules.pop("memoryd", None)
            else:
                sys.modules["memoryd"] = daemon_before
    if import_error is not None:
        return errors + [
            f"memoryd plugin import failed: {type(import_error).__name__}: {import_error}"
        ]
    assert plugin is not None
    provider_class = getattr(plugin, "MemorydProvider", None)
    if not inspect.isclass(provider_class):
        return errors + ["memoryd plugin does not export MemorydProvider"]
    if not issubclass(provider_class, checked_class):
        errors.append("MemorydProvider is not a subclass of the checked MemoryProvider")
    remaining = sorted(provider_class.__abstractmethods__)
    if remaining:
        errors.append(f"MemorydProvider has unimplemented abstract methods: {remaining}")
    try:
        provider = provider_class()
    except TypeError as exc:
        errors.append(f"MemorydProvider is not instantiable: {exc}")
        return errors
    if not isinstance(provider.name, str) or not provider.name:
        errors.append("MemorydProvider.name must be a non-empty string")
    for name in REQUIRED_METHODS:
        member = getattr(provider, name, None)
        if not callable(member):
            errors.append(f"MemorydProvider lifecycle/config/tool method {name} is missing")
            continue
        base_member = inspect.getattr_static(checked_class, name, None)
        plugin_member = inspect.getattr_static(provider_class, name, None)
        expected = _member_signature(base_member)
        actual = _member_signature(plugin_member)
        if expected != actual:
            errors.append(
                f"MemorydProvider signature mismatch for {name}: "
                f"contract {expected}; plugin {actual}"
            )
    schemas = provider.get_tool_schemas()
    if not isinstance(schemas, list) or any(not isinstance(item, dict) for item in schemas):
        errors.append("MemorydProvider.get_tool_schemas() must return a list of objects")
    elif any(
        not {"name", "description", "parameters"}.issubset(item) for item in schemas
    ):
        errors.append("MemorydProvider tool schemas must include name/description/parameters")
    config = provider.get_config_schema()
    if not isinstance(config, list) or any(not isinstance(item, dict) for item in config):
        errors.append("MemorydProvider.get_config_schema() must return a list of objects")
    if provider.is_available() is not True:
        errors.append("MemorydProvider.is_available() must be true without DB/network access")
    return errors


def check_contract(
    source_root: Path | None = None, *, plugin_path: Path = PLUGIN_PATH
) -> list[str]:
    saved_agent = sys.modules.get("agent")
    saved_provider = sys.modules.get("agent.memory_provider")
    saved_spool_modules = {
        name for name in sys.modules if name.startswith("_hermes_memoryd_spool_")
    }
    try:
        pinned_module = _load_file("_memoryd_pinned_hermes_contract", PINNED_CONTRACT)
        if source_root is None:
            checked_module, _ = _load_installed_contract()
        else:
            sys.modules.pop("agent.memory_provider", None)
            sys.modules.pop("agent", None)
            checked_module, _ = _load_source_contract(source_root)
        pinned_class = pinned_module.MemoryProvider
        checked_class = checked_module.MemoryProvider
        errors = _compare_contracts(pinned_class, checked_class)
        errors.extend(_validate_plugin(checked_class, plugin_path))
        return errors
    finally:
        sys.modules.pop("_memoryd_pinned_hermes_contract", None)
        sys.modules.pop("_memoryd_hermes_contract_plugin", None)
        for name in tuple(sys.modules):
            if (
                name.startswith("_hermes_memoryd_spool_")
                and name not in saved_spool_modules
            ):
                sys.modules.pop(name, None)
        sys.modules.pop("agent.memory_provider", None)
        sys.modules.pop("agent", None)
        if saved_agent is not None:
            sys.modules["agent"] = saved_agent
        if saved_provider is not None:
            sys.modules["agent.memory_provider"] = saved_provider


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        help="Hermes Agent checkout root; default checks installed agent.memory_provider",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    checked = (
        args.source_root.resolve() / "agent" / "memory_provider.py"
        if args.source_root
        else "installed agent.memory_provider"
    )
    print(
        f"Pinned Hermes contract: tag {PINNED_TAG}, commit {PINNED_COMMIT}, "
        "agent/memory_provider.py"
    )
    print(f"Checked Hermes contract: {checked}")
    try:
        errors = check_contract(args.source_root)
    except Exception as exc:
        print(f"INCOMPATIBLE: contract check could not run: {type(exc).__name__}: {exc}")
        return 1
    if errors:
        print("INCOMPATIBLE: memoryd does not satisfy the checked Hermes contract")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("COMPATIBLE: memoryd satisfies the checked Hermes contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
