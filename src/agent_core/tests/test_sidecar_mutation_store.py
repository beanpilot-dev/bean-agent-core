"""Focused persistence-boundary tests for sidecar mutation replay."""

from datetime import date
from pathlib import Path

import pytest

from agent_core.services.approvals.contracts import PendingActionService
from agent_core.services.ledger import LedgerService
from agent_core.services.mutations import (
    FilesystemSidecarMutationStore,
    MutationCoordinator,
    MutationExecutor,
    MutationOperation,
    MutationPlan,
    MutationPlanner,
)
from agent_core.services.mutations.facts import capture_account_state_fact
from agent_core.services.mutations.plans import FilePrecondition
from agent_core.services.types import IntegrityFailed, LedgerConfig


def test_store_restores_new_sidecar_files_and_directories(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "main.beancount").write_text(
        'option "operating_currency" "CNY"\ninclude "agent_inc/main.beancount"\n'
    )
    config = LedgerConfig()
    store = FilesystemSidecarMutationStore()
    paths = [
        config.sidecar_main_path,
        f"{config.sidecar_write_dir}/{date.today():%Y-%m}.beancount",
    ]
    captured = store.snapshot(str(tmp_path), paths, config)

    store.append(
        str(tmp_path),
        '2026-06-15 * "Dinner"\n  Expenses:Food:Dining  10 CNY\n  Assets:Cash -10 CNY',
        config,
    )
    store.restore(str(tmp_path), captured, config)

    assert not (data / "agent_inc").exists()


def test_store_rejects_reads_and_replacements_outside_configured_sidecar(
    ledger_workspace: Path,
) -> None:
    store = FilesystemSidecarMutationStore()

    with pytest.raises(ValueError, match="sidecar_write_dir"):
        store.snapshot(str(ledger_workspace), ["data/main.beancount"])
    with pytest.raises(ValueError, match="sidecar_write_dir"):
        store.replace(
            str(ledger_workspace),
            "data/main.beancount",
            "option",
            "option",
        )


def test_store_rejects_symlinked_sidecar_during_validation_and_apply(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "data").mkdir()
    (workspace / "data" / "agent_inc").symlink_to(outside, target_is_directory=True)
    store = FilesystemSidecarMutationStore()

    with pytest.raises(ValueError, match="symlink"):
        store.append(str(workspace), "2026-06-15 custom test")

    assert list(outside.iterdir()) == []


def test_validation_copy_preserves_symlinks_for_identical_rejection(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    outside = tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (source / "data").mkdir()
    (source / "data" / "agent_inc").symlink_to(outside, target_is_directory=True)
    target = tmp_path / "target"

    FilesystemSidecarMutationStore().copy_workspace(str(source), str(target))

    assert (target / "data" / "agent_inc").is_symlink()


def test_approved_plan_outside_sidecar_fails_closed(ledger_workspace: Path) -> None:
    plan = MutationPlan.from_operations(
        [
            MutationOperation(
                kind="replace",
                target_file="data/main.beancount",
                old_text='option "title" "Test Ledger"',
                text='option "title" "Changed"',
            )
        ],
        commit_message="invalid legacy update",
        remediation="Prepare a sidecar-only correction.",
    ).with_preconditions(
        [
            FilePrecondition.from_content(
                "data/main.beancount",
                (ledger_workspace / "data" / "main.beancount").read_text(),
            )
        ]
    )
    pending = PendingActionService.create_pending_action(
        action_type="update_transaction",
        execution_spec={"mutation_plan": plan.to_spec()},
        display={"diff": "invalid"},
        validation={"status": "validated"},
    )
    publisher = type(
        "Publisher",
        (),
        {"commit_and_push": lambda *_args: pytest.fail("must not publish")},
    )()

    result = LedgerService().apply_pending_action(
        str(ledger_workspace), pending.__dict__.copy(), "repo", publisher
    )

    assert isinstance(result, IntegrityFailed)
    assert "sidecar write isolation" in result.error


def test_approved_append_plan_fails_closed_when_layout_changes(
    ledger_workspace: Path,
) -> None:
    sealed = MutationCoordinator.seal(
        str(ledger_workspace),
        MutationPlanner.commit(
            '2026-06-15 * "Dinner"\n'
            "  Expenses:Food:Dining  10 CNY\n"
            "  Assets:Cash          -10 CNY",
            "record dinner",
        ),
    )
    pending = PendingActionService.create_pending_action(
        action_type="commit_transaction",
        execution_spec={"mutation_plan": sealed.to_spec()},
        display={"diff": "dinner"},
        validation={"status": "validated"},
    )
    publisher = type(
        "Publisher",
        (),
        {"commit_and_push": lambda *_args: pytest.fail("must not publish")},
    )()
    changed_layout = LedgerConfig(
        entry_path="data/main.beancount",
        sidecar_main_path="data/other_sidecar/main.beancount",
        sidecar_write_dir="data/other_sidecar",
    )

    result = LedgerService().apply_pending_action(
        str(ledger_workspace),
        pending.__dict__.copy(),
        "repo",
        publisher,
        ledger_config=changed_layout,
    )

    assert isinstance(result, IntegrityFailed)
    assert "write set" in result.error
    assert not (ledger_workspace / "data" / "other_sidecar").exists()


def test_preconditions_recompute_semantic_facts_with_active_custom_config(
    tmp_path: Path,
) -> None:
    books = tmp_path / "books"
    sidecar = books / "agent_sidecar"
    sidecar.mkdir(parents=True)
    config = LedgerConfig(
        entry_path="books/root.beancount",
        sidecar_main_path="books/agent_sidecar/main.beancount",
        sidecar_write_dir="books/agent_sidecar",
    )
    (books / "root.beancount").write_text('include "agent_sidecar/main.beancount"\n')
    (sidecar / "main.beancount").write_text(
        "2020-01-01 open Assets:Cash CNY\n"
        "2020-01-01 open Expenses:Food:Dining CNY\n"
    )
    fact = capture_account_state_fact(str(tmp_path), "Assets:Cash", config)
    plan = MutationPlanner.commit(
        '2026-06-15 * "Dinner"\n'
        "  Expenses:Food:Dining  10 CNY\n"
        "  Assets:Cash          -10 CNY",
        "record dinner",
    ).with_semantic_facts((fact,))

    assert MutationExecutor.preconditions_hold(str(tmp_path), plan, config)
