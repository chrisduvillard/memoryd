#!/usr/bin/env python3
"""DB-free tests for the pinned Hermes MemoryProvider contract checker."""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CHECKER = REPO / "scripts" / "check_hermes_contract.py"
PINNED = REPO / "memoryd" / "hermes_validation" / "agent" / "memory_provider.py"
PLUGIN = REPO / "hermes_plugin" / "memoryd" / "__init__.py"


def _daemon_state() -> tuple[bool, object | None]:
    return "memoryd" in sys.modules, sys.modules.get("memoryd")


def _assert_daemon_state(
        case: unittest.TestCase, before: tuple[bool, object | None]) -> None:
    present, module = before
    if present:
        case.assertIs(sys.modules.get("memoryd"), module)
    else:
        case.assertNotIn("memoryd", sys.modules)


def _managed_import_state() -> dict[str, object]:
    return {
        name: module for name, module in sys.modules.items()
        if (name == "agent" or name.startswith("agent.") or
            name == "_memoryd_hermes_contract_plugin" or
            name.startswith("_hermes_memoryd_spool_"))
    }


def _assert_managed_import_state(
        case: unittest.TestCase, before: dict[str, object]) -> None:
    after = _managed_import_state()
    case.assertEqual(set(after), set(before))
    for name, module in before.items():
        case.assertIs(after[name], module)


class _ContractMutation(ast.NodeTransformer):
    def __init__(self, method: str, action: str, class_name: str) -> None:
        self.method = method
        self.action = action
        self.class_name = class_name

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        if node.name != self.class_name:
            return self.generic_visit(node)
        if self.action == "remove":
            node.body = [
                item for item in node.body
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                or item.name != self.method
            ]
        elif self.action == "change-signature":
            method = next(
                item for item in node.body
                if isinstance(item, ast.FunctionDef) and item.name == self.method
            )
            method.args.kwarg = None
        elif self.action == "remove-parameter":
            method = next(
                item for item in node.body
                if isinstance(item, ast.FunctionDef) and item.name == self.method
            )
            method.args.args = [
                parameter
                for parameter in method.args.args
                if parameter.arg != "metadata"
            ]
        elif self.action == "remove-property":
            method = next(
                item for item in node.body
                if isinstance(item, ast.FunctionDef) and item.name == self.method
            )
            method.decorator_list = [
                decorator
                for decorator in method.decorator_list
                if not isinstance(decorator, ast.Name) or decorator.id != "property"
            ]
        elif self.action in {"add-staticmethod", "add-classmethod"}:
            method = next(
                item for item in node.body
                if isinstance(item, ast.FunctionDef) and item.name == self.method
            )
            method.decorator_list.insert(0, ast.Name(id=self.action.removeprefix("add-")))
        elif self.action == "add-abstract":
            extra = ast.parse(
                "class Added:\n"
                "    @abstractmethod\n"
                "    def new_required_hook(self, value: str) -> None:\n"
                "        raise NotImplementedError\n"
            ).body[0].body[0]
            node.body.append(extra)
        elif self.action == "add-concrete":
            extra = ast.parse(
                "class Added:\n"
                "    def new_optional_hook(self, value: str = '') -> None:\n"
                "        pass\n"
            ).body[0].body[0]
            node.body.append(extra)
        return self.generic_visit(node)


def _mutate(
    source: str, method: str, action: str, class_name: str = "MemoryProvider"
) -> str:
    tree = _ContractMutation(method, action, class_name).visit(ast.parse(source))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _make_async(source: str, method: str, class_name: str) -> str:
    tree = ast.parse(source)
    target = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    for index, node in enumerate(target.body):
        if isinstance(node, ast.FunctionDef) and node.name == method:
            replacement = ast.AsyncFunctionDef(
                **{field: getattr(node, field) for field in node._fields}
            )
            target.body[index] = ast.copy_location(replacement, node)
            break
    else:
        raise AssertionError(f"missing {class_name}.{method}")
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


class HermesContractCheckerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "hermes-agent"
        agent = self.root / "agent"
        agent.mkdir(parents=True)
        (agent / "__init__.py").write_text("", encoding="utf-8")
        self.pinned_source = PINNED.read_text(encoding="utf-8")

    def tearDown(self) -> None:
        for name in tuple(sys.modules):
            if name == "agent" or name.startswith("agent."):
                sys.modules.pop(name, None)
        self.tmp.cleanup()

    def _write_contract(self, source: str) -> None:
        (self.root / "agent" / "memory_provider.py").write_text(
            source, encoding="utf-8"
        )

    def _run(self, *extra_args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(CHECKER),
                "--source-root",
                str(self.root),
                *extra_args,
            ],
            cwd=REPO,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_pinned_snapshot_is_compatible(self) -> None:
        self._write_contract(self.pinned_source)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("COMPATIBLE", result.stdout)

    def test_removed_abstract_method_is_incompatible(self) -> None:
        self._write_contract(_mutate(self.pinned_source, "is_available", "remove"))
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is_available", result.stdout + result.stderr)

    def test_added_abstract_method_is_incompatible(self) -> None:
        self._write_contract(_mutate(self.pinned_source, "", "add-abstract"))
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("new_required_hook", result.stdout + result.stderr)

    def test_changed_required_signature_is_incompatible(self) -> None:
        self._write_contract(
            _mutate(self.pinned_source, "initialize", "change-signature")
        )
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("initialize", result.stdout + result.stderr)
        self.assertIn("signature", (result.stdout + result.stderr).lower())

    def test_added_optional_concrete_hook_is_compatible(self) -> None:
        self._write_contract(_mutate(self.pinned_source, "", "add-concrete"))
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_candidate_async_change_is_incompatible(self) -> None:
        self._write_contract(
            _make_async(self.pinned_source, "prefetch", "MemoryProvider")
        )
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prefetch", result.stdout + result.stderr)
        self.assertIn("async", (result.stdout + result.stderr).lower())

    def test_candidate_abstract_property_removed_is_incompatible(self) -> None:
        self._write_contract(
            _mutate(self.pinned_source, "name", "remove-property")
        )
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("name", result.stdout + result.stderr)
        self.assertIn("descriptor", (result.stdout + result.stderr).lower())

    def test_candidate_static_and_class_descriptor_changes_are_incompatible(self) -> None:
        for descriptor in ("staticmethod", "classmethod"):
            with self.subTest(descriptor=descriptor):
                self._write_contract(
                    _mutate(self.pinned_source, "prefetch", f"add-{descriptor}")
                )
                result = self._run()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("descriptor", (result.stdout + result.stderr).lower())

    def test_annotation_spelling_changes_are_compatible(self) -> None:
        self._write_contract(
            self.pinned_source.replace("Dict[str, Any]", "dict[str, Any]")
        )
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_source_root_contract_is_never_executed(self) -> None:
        marker = Path(self.tmp.name) / "source-executed"
        self._write_contract(
            self.pinned_source
            + f"\nopen({str(marker)!r}, 'w').write('executed')\n"
        )
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(marker.exists())

    def test_require_pinned_bytes_rejects_semantic_only_match(self) -> None:
        self._write_contract(self.pinned_source + "\n# byte drift\n")
        result = self._run("--require-pinned-bytes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("byte identity mismatch", (result.stdout + result.stderr).lower())

    def test_plugin_no_longer_instantiable_is_incompatible(self) -> None:
        self._write_contract(self.pinned_source)
        broken_dir = Path(self.tmp.name) / "broken_plugin"
        broken_dir.mkdir()
        broken_plugin = broken_dir / "__init__.py"
        (broken_dir / "spool.py").write_text(
            (PLUGIN.with_name("spool.py")).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        broken_plugin.write_text(
            _mutate(
                PLUGIN.read_text(encoding="utf-8"),
                "is_available",
                "remove",
                "MemorydProvider",
            ),
            encoding="utf-8",
        )
        spec = importlib.util.spec_from_file_location("hermes_contract_checker", CHECKER)
        checker = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)
        spec.loader.exec_module(checker)
        daemon_before = _daemon_state()
        errors = checker.check_contract(self.root, plugin_path=broken_plugin)
        self.assertTrue(any("abstract" in error.lower() for error in errors), errors)
        _assert_daemon_state(self, daemon_before)

    def test_changed_concrete_hook_override_is_incompatible(self) -> None:
        self._write_contract(self.pinned_source)
        broken_dir = Path(self.tmp.name) / "changed_hook_plugin"
        broken_dir.mkdir()
        broken_plugin = broken_dir / "__init__.py"
        (broken_dir / "spool.py").write_text(
            PLUGIN.with_name("spool.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        broken_plugin.write_text(
            _mutate(
                PLUGIN.read_text(encoding="utf-8"),
                "on_memory_write",
                "remove-parameter",
                "MemorydProvider",
            ),
            encoding="utf-8",
        )
        checker = self._load_checker()
        errors = checker.check_contract(self.root, plugin_path=broken_plugin)
        self.assertTrue(
            any("on_memory_write" in error and "signature" in error for error in errors),
            errors,
        )

    def test_plugin_async_override_mismatch_is_incompatible(self) -> None:
        self._write_contract(self.pinned_source)
        plugin_dir = Path(self.tmp.name) / "async_plugin"
        plugin_dir.mkdir()
        plugin_path = plugin_dir / "__init__.py"
        (plugin_dir / "spool.py").write_text(
            PLUGIN.with_name("spool.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        plugin_path.write_text(
            _make_async(
                PLUGIN.read_text(encoding="utf-8"),
                "prefetch",
                "MemorydProvider",
            ),
            encoding="utf-8",
        )
        checker = self._load_checker()
        errors = checker.check_contract(self.root, plugin_path=plugin_path)
        self.assertTrue(
            any("prefetch" in error and "async" in error for error in errors), errors
        )

    def test_plugin_property_override_mismatch_is_incompatible(self) -> None:
        self._write_contract(self.pinned_source)
        plugin_dir = Path(self.tmp.name) / "property_plugin"
        plugin_dir.mkdir()
        plugin_path = plugin_dir / "__init__.py"
        (plugin_dir / "spool.py").write_text(
            PLUGIN.with_name("spool.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        plugin_path.write_text(
            _mutate(
                PLUGIN.read_text(encoding="utf-8"),
                "name",
                "remove-property",
                "MemorydProvider",
            ),
            encoding="utf-8",
        )
        checker = self._load_checker()
        errors = checker.check_contract(self.root, plugin_path=plugin_path)
        self.assertTrue(
            any("name" in error and "descriptor" in error for error in errors), errors
        )

    def test_plugin_static_and_class_override_mismatches_are_incompatible(self) -> None:
        self._write_contract(self.pinned_source)
        checker = self._load_checker()
        for descriptor in ("staticmethod", "classmethod"):
            with self.subTest(descriptor=descriptor):
                plugin_dir = Path(self.tmp.name) / f"{descriptor}_plugin"
                plugin_dir.mkdir()
                plugin_path = plugin_dir / "__init__.py"
                (plugin_dir / "spool.py").write_text(
                    PLUGIN.with_name("spool.py").read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                plugin_path.write_text(
                    _mutate(
                        PLUGIN.read_text(encoding="utf-8"),
                        "prefetch",
                        f"add-{descriptor}",
                        "MemorydProvider",
                    ),
                    encoding="utf-8",
                )
                errors = checker.check_contract(self.root, plugin_path=plugin_path)
                self.assertTrue(
                    any(
                        "prefetch" in error and "descriptor" in error
                        for error in errors
                    ),
                    errors,
                )

    def test_synthetic_modules_are_cleaned_after_success_and_failure(self) -> None:
        self._write_contract(self.pinned_source)
        checker = self._load_checker()
        for suffix in ("success", "failure"):
            plugin_dir = Path(self.tmp.name) / f"leaky_plugin_{suffix}"
            plugin_dir.mkdir()
            plugin_path = plugin_dir / "__init__.py"
            (plugin_dir / "spool.py").write_text(
                PLUGIN.with_name("spool.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            source = PLUGIN.read_text(encoding="utf-8")
            source += "\nsys.modules['agent.helper'] = type(sys)('agent.helper')\n"
            if suffix == "failure":
                source += "sys.modules['memoryd'] = type(sys)('memoryd')\n"
                source += "raise RuntimeError('plugin import failure')\n"
            plugin_path.write_text(source, encoding="utf-8")
            daemon_before = _daemon_state()
            managed_before = _managed_import_state()
            errors = checker.check_contract(self.root, plugin_path=plugin_path)
            if suffix == "failure":
                self.assertTrue(
                    any("daemon package" in error for error in errors), errors
                )
                _assert_daemon_state(self, daemon_before)
            _assert_managed_import_state(self, managed_before)

    def test_preserves_preexisting_legitimate_import_modules(self) -> None:
        self._write_contract(self.pinned_source)
        checker = self._load_checker()
        import memoryd as daemon

        names = ("agent", "agent.helper")
        previous = {name: sys.modules.get(name) for name in names}
        present = {name for name in names if name in sys.modules}
        agent = types.ModuleType("agent")
        helper = types.ModuleType("agent.helper")
        sys.modules["agent"] = agent
        sys.modules["agent.helper"] = helper
        try:
            errors = checker.check_contract(self.root)
            self.assertEqual(errors, [])
            self.assertIs(sys.modules.get("memoryd"), daemon)
            self.assertIs(sys.modules.get("agent"), agent)
            self.assertIs(sys.modules.get("agent.helper"), helper)
        finally:
            for name in names:
                if name in present:
                    sys.modules[name] = previous[name]
                else:
                    sys.modules.pop(name, None)

    def test_workflow_pins_actions_and_never_executes_upstream_main(self) -> None:
        workflow = (REPO / ".github" / "workflows" / "tests.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("actions/checkout@v", workflow)
        self.assertNotIn("actions/setup-python@v", workflow)
        self.assertIn("actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5", workflow)
        self.assertIn("actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065", workflow)
        advisory = workflow.split("  upstream-hermes-advisory:", 1)[1].split(
            "  hermes-spool:", 1
        )[0]
        self.assertNotIn("HERMES_SOURCE_ROOT", advisory)
        self.assertNotIn("test_hermes_spool.py", advisory)
        self.assertNotIn("\n    continue-on-error: true\n", advisory)
        self.assertIn("\n        continue-on-error: true\n", advisory)
        self.assertIn("steps.compatibility.outcome", advisory)

    @staticmethod
    def _load_checker():
        spec = importlib.util.spec_from_file_location("hermes_contract_checker", CHECKER)
        checker = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(checker)
        return checker


if __name__ == "__main__":
    unittest.main(verbosity=2)
