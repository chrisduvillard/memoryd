#!/usr/bin/env python3
"""Check memoryd against the pinned Hermes Agent MemoryProvider contract."""
from __future__ import annotations

import argparse
import ast
import inspect
import sys
import types
from pathlib import Path
from typing import Any, NamedTuple


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PINNED_CONTRACT = Path(__file__).resolve().parent / "agent" / "memory_provider.py"
_PACKAGED_PLUGIN = PACKAGE_ROOT / "hermes_plugin"
_SOURCE_PLUGIN = PACKAGE_ROOT.parent / "hermes_plugin" / "memoryd"
PLUGIN_PATH = (
    _PACKAGED_PLUGIN if _PACKAGED_PLUGIN.is_dir() else _SOURCE_PLUGIN
) / "__init__.py"
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
MISSING_DEFAULT = ("missing", "")


class ParameterContract(NamedTuple):
    name: str
    kind: str
    default: tuple[str, str]


class MethodContract(NamedTuple):
    parameters: tuple[ParameterContract, ...]
    abstract: bool
    asynchronous: bool
    descriptor: str


def _managed_import_name(name: str) -> bool:
    return (
        name == "agent"
        or name.startswith("agent.")
        or name == "_memoryd_hermes_contract_plugin"
        or name.startswith("_hermes_memoryd_spool_")
    )


def _clean_import_modules() -> None:
    for name in tuple(sys.modules):
        if _managed_import_name(name):
            sys.modules.pop(name, None)


def _load_file(module_name: str, path: Path):
    import importlib.util

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


def _default_key(value: Any) -> tuple[str, str]:
    return (type(value).__qualname__, repr(value))


def _ast_default(node: ast.expr | None) -> tuple[str, str]:
    if node is None:
        return MISSING_DEFAULT
    try:
        return _default_key(ast.literal_eval(node))
    except (ValueError, TypeError):
        return ("expression", ast.dump(node, include_attributes=False))


def _ast_parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[ParameterContract, ...]:
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    first_default = len(positional) - len(args.defaults)
    parameters: list[ParameterContract] = []
    for index, parameter in enumerate(positional):
        default = (
            MISSING_DEFAULT
            if index < first_default
            else _ast_default(args.defaults[index - first_default])
        )
        kind = (
            "POSITIONAL_ONLY"
            if index < len(args.posonlyargs)
            else "POSITIONAL_OR_KEYWORD"
        )
        parameters.append(ParameterContract(parameter.arg, kind, default))
    if args.vararg is not None:
        parameters.append(
            ParameterContract(args.vararg.arg, "VAR_POSITIONAL", MISSING_DEFAULT)
        )
    for parameter, default_node in zip(args.kwonlyargs, args.kw_defaults):
        parameters.append(
            ParameterContract(parameter.arg, "KEYWORD_ONLY", _ast_default(default_node))
        )
    if args.kwarg is not None:
        parameters.append(
            ParameterContract(args.kwarg.arg, "VAR_KEYWORD", MISSING_DEFAULT)
        )
    return tuple(parameters)


def _decorator_name(decorator: ast.expr) -> str | None:
    if isinstance(decorator, ast.Name):
        return decorator.id
    if isinstance(decorator, ast.Attribute):
        return decorator.attr
    return None


def _is_abstract(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        _decorator_name(decorator) == "abstractmethod"
        for decorator in node.decorator_list
    )


def _ast_descriptor(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    decorators = {_decorator_name(item) for item in node.decorator_list}
    for descriptor in ("property", "staticmethod", "classmethod"):
        if descriptor in decorators:
            return descriptor
    return "instance method"


def _parse_contract(path: Path) -> dict[str, MethodContract]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    provider = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "MemoryProvider"
        ),
        None,
    )
    if provider is None:
        raise ValueError(f"{path} does not define MemoryProvider")
    contract: dict[str, MethodContract] = {}
    for node in provider.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue
        contract[node.name] = MethodContract(
            _ast_parameters(node),
            _is_abstract(node),
            isinstance(node, ast.AsyncFunctionDef),
            _ast_descriptor(node),
        )
    return contract


def _runtime_method(member: Any) -> MethodContract | None:
    descriptor = "instance method"
    if isinstance(member, property):
        descriptor = "property"
        member = member.fget
    elif isinstance(member, staticmethod):
        descriptor = "staticmethod"
        member = member.__func__
    elif isinstance(member, classmethod):
        descriptor = "classmethod"
        member = member.__func__
    if not callable(member):
        return None
    parameters: list[ParameterContract] = []
    for parameter in inspect.signature(member, eval_str=False).parameters.values():
        default = (
            MISSING_DEFAULT
            if parameter.default is inspect.Parameter.empty
            else _default_key(parameter.default)
        )
        parameters.append(
            ParameterContract(parameter.name, parameter.kind.name, default)
        )
    return MethodContract(
        tuple(parameters),
        bool(getattr(member, "__isabstractmethod__", False)),
        inspect.iscoroutinefunction(member),
        descriptor,
    )


def _format_parameters(parameters: tuple[ParameterContract, ...]) -> str:
    return repr([(item.name, item.kind, item.default) for item in parameters])


def _compare_contracts(
    pinned: dict[str, MethodContract], checked: dict[str, MethodContract]
) -> list[str]:
    errors: list[str] = []
    for name, pinned_method in pinned.items():
        checked_method = checked.get(name)
        if checked_method is None:
            errors.append(f"contract removed public method/property {name}")
            continue
        if checked_method.parameters != pinned_method.parameters:
            errors.append(
                f"contract signature changed for {name}: pinned "
                f"{_format_parameters(pinned_method.parameters)}; checked "
                f"{_format_parameters(checked_method.parameters)}"
            )
        if checked_method.asynchronous != pinned_method.asynchronous:
            errors.append(
                f"contract async/sync changed for {name}: "
                f"pinned async={pinned_method.asynchronous}, "
                f"checked async={checked_method.asynchronous}"
            )
        if checked_method.descriptor != pinned_method.descriptor:
            errors.append(
                f"contract descriptor changed for {name}: "
                f"pinned={pinned_method.descriptor}, "
                f"checked={checked_method.descriptor}"
            )
        if checked_method.abstract != pinned_method.abstract:
            errors.append(
                f"contract abstract status changed for {name}: "
                f"pinned={pinned_method.abstract}, checked={checked_method.abstract}"
            )
    pinned_abstracts = {name for name, method in pinned.items() if method.abstract}
    checked_abstracts = {name for name, method in checked.items() if method.abstract}
    for name in sorted(checked_abstracts - pinned_abstracts):
        errors.append(f"contract added abstract method/property {name}")
    for name in sorted(pinned_abstracts - checked_abstracts):
        errors.append(f"contract removed pinned abstract requirement {name}")
    return errors


def _load_plugin_against_pinned(plugin_path: Path):
    agent_package = types.ModuleType("agent")
    agent_package.__path__ = [str(PINNED_CONTRACT.parent)]
    sys.modules["agent"] = agent_package
    pinned_module = _load_file("agent.memory_provider", PINNED_CONTRACT)
    plugin = _load_file("_memoryd_hermes_contract_plugin", plugin_path.resolve())
    return pinned_module.MemoryProvider, plugin


def _validate_plugin(
    checked: dict[str, MethodContract], plugin_path: Path
) -> list[str]:
    errors: list[str] = []
    daemon_before = sys.modules.get("memoryd")
    plugin = None
    pinned_class = None
    import_error = None
    try:
        pinned_class, plugin = _load_plugin_against_pinned(plugin_path)
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
    assert pinned_class is not None and plugin is not None
    provider_class = getattr(plugin, "MemorydProvider", None)
    if not inspect.isclass(provider_class):
        return errors + ["memoryd plugin does not export MemorydProvider"]
    if not issubclass(provider_class, pinned_class):
        errors.append("MemorydProvider is not a subclass of pinned MemoryProvider")
    remaining = sorted(provider_class.__abstractmethods__)
    if remaining:
        errors.append(f"MemorydProvider has unimplemented abstract methods: {remaining}")
    try:
        provider = provider_class()
    except TypeError as exc:
        errors.append(f"MemorydProvider is not instantiable: {exc}")
        return errors
    for name, checked_method in checked.items():
        if name not in vars(provider_class):
            if checked_method.abstract:
                errors.append(f"MemorydProvider does not override abstract method {name}")
            continue
        override = inspect.getattr_static(provider_class, name)
        actual = _runtime_method(override)
        if actual is None:
            errors.append(f"MemorydProvider override {name} is not callable")
            continue
        if actual.descriptor != checked_method.descriptor:
            errors.append(
                f"MemorydProvider descriptor mismatch for {name}: contract "
                f"{checked_method.descriptor}; plugin {actual.descriptor}"
            )
        if actual.asynchronous != checked_method.asynchronous:
            errors.append(
                f"MemorydProvider async/sync mismatch for {name}: contract "
                f"async={checked_method.asynchronous}; plugin "
                f"async={actual.asynchronous}"
            )
        if actual.parameters != checked_method.parameters:
            errors.append(
                f"MemorydProvider signature mismatch for {name}: contract "
                f"{_format_parameters(checked_method.parameters)}; plugin "
                f"{_format_parameters(actual.parameters)}"
            )
    if not isinstance(provider.name, str) or not provider.name:
        errors.append("MemorydProvider.name must be a non-empty string")
    for name in REQUIRED_METHODS:
        if not callable(getattr(provider, name, None)):
            errors.append(f"MemorydProvider lifecycle/config/tool method {name} is missing")
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


def _installed_contract_path() -> Path:
    for entry in sys.path:
        root = Path(entry or ".").resolve()
        candidate = root / "agent" / "memory_provider.py"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "cannot locate installed agent/memory_provider.py on sys.path"
    )


def check_contract(
    source_root: Path | None = None,
    *,
    plugin_path: Path = PLUGIN_PATH,
    require_pinned_bytes: bool = False,
) -> list[str]:
    managed_before = {
        name: module for name, module in sys.modules.items()
        if _managed_import_name(name)
    }
    _clean_import_modules()
    try:
        checked_path = (
            source_root.resolve() / "agent" / "memory_provider.py"
            if source_root is not None
            else _installed_contract_path()
        )
        if not checked_path.is_file():
            raise FileNotFoundError(
                f"Hermes source root has no agent/memory_provider.py: {source_root}"
            )
        errors: list[str] = []
        if require_pinned_bytes and checked_path.read_bytes() != PINNED_CONTRACT.read_bytes():
            errors.append(
                "pinned byte identity mismatch for agent/memory_provider.py"
            )
        pinned = _parse_contract(PINNED_CONTRACT)
        checked = _parse_contract(checked_path)
        errors.extend(_compare_contracts(pinned, checked))
        errors.extend(_validate_plugin(checked, plugin_path))
        return errors
    finally:
        _clean_import_modules()
        sys.modules.update(managed_before)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        type=Path,
        help="Hermes Agent checkout root; default checks installed source on sys.path",
    )
    parser.add_argument(
        "--require-pinned-bytes",
        action="store_true",
        help="also require source agent/memory_provider.py to byte-match the pin",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    checked = (
        args.source_root.resolve() / "agent" / "memory_provider.py"
        if args.source_root
        else "installed agent.memory_provider source"
    )
    print(
        f"Pinned Hermes contract: tag {PINNED_TAG}, commit {PINNED_COMMIT}, "
        "agent/memory_provider.py"
    )
    print(f"Checked Hermes contract: {checked}")
    try:
        errors = check_contract(
            args.source_root,
            require_pinned_bytes=args.require_pinned_bytes,
        )
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
