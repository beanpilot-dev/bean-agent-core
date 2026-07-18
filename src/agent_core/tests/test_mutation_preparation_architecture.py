"""Structural guardrails for the decomposed mutation preparation boundary."""

from __future__ import annotations

import ast
from pathlib import Path

from agent_core.services.mutations.handlers import (
    MutationPreparationHandlerRegistry,
    PreparedMutation,
)

_SERVICES = Path(__file__).parents[1] / "services"
_MUTATIONS = _SERVICES / "mutations"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_preparation_shell_has_no_domain_or_filesystem_dependencies() -> None:
    path = _MUTATIONS / "preparation.py"
    source = path.read_text(encoding="utf-8")
    tree = _tree(path)
    imported = _imported_modules(tree)

    forbidden_modules = {
        "os",
        "re",
        "shutil",
        "tempfile",
        "decimal",
        "beancount",
    }
    assert not {module.split(".")[0] for module in imported} & forbidden_modules
    assert not any(
        marker in source
        for marker in (
            "LedgerQueryService",
            "GitService",
            "loader.",
            "bean_check",
            "bean_format",
            "_apply_plan",
            "confirm_",
        )
    )


def test_preparation_shell_does_not_branch_on_action_identity() -> None:
    tree = _tree(_MUTATIONS / "preparation.py")
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp, ast.Match)):
            branch = ast.unparse(node)
            assert "action_type" not in branch
            assert "handler_key" not in branch


def test_registry_is_the_only_nine_action_extension_point() -> None:
    assert MutationPreparationHandlerRegistry().keys() == (
        "commit_transaction",
        "open_account",
        "update_transaction",
        "delete_transaction",
        "price",
        "bulk_commit",
        "change_set",
        "balance_reconciliation",
        "balance_update",
    )
    callable_members = {
        name
        for name, value in vars(PreparedMutation).items()
        if callable(value) and not name.startswith("__")
    }
    assert callable_members == set()


def test_application_replays_plans_without_preparation_or_legacy_paths() -> None:
    path = _MUTATIONS / "application.py"
    source = path.read_text(encoding="utf-8")
    imported = _imported_modules(_tree(path))

    assert not any(module.endswith("preparation") for module in imported)
    assert "confirm_" not in source
    assert "_apply_legacy" not in source
    assert "_apply_plan" not in source
    assert "mutation_plan" in source


def test_ledger_facade_is_explicit_and_duplicate_mutation_inspection_is_gone() -> None:
    ledger_tree = _tree(_SERVICES / "ledger.py")
    method_names = {
        node.name for node in ast.walk(ledger_tree) if isinstance(node, ast.FunctionDef)
    }

    assert "__getattr__" not in method_names
    assert not any(name.startswith("confirm_") for name in method_names)
    assert not (_MUTATIONS / "inspection.py").exists()
