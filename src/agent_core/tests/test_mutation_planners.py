"""Characterization tests for canonical action-specific mutation plans."""

from pathlib import Path

from agent_core.services.ledger import LedgerService
from agent_core.services.mutations import MutationCoordinator, MutationOperation, MutationPlanner
from agent_core.services.mutations.action_handlers import (
    MutationPreparationHandlerRegistry,
    PreparedMutation,
)
from agent_core.services.types import PendingAction


def test_core_planners_preserve_operation_and_remediation_contracts() -> None:
    commit = MutationPlanner.commit("transaction", "record transaction")
    opened = MutationPlanner.open_account("Assets:Cash:Wallet", "open directive")
    updated = MutationPlanner.transaction_update(
        "data/agent_inc/current.beancount", "old", "new", "update"
    )
    bulk = MutationPlanner.bulk("transactions", "import")

    assert commit.operations == (MutationOperation(kind="append", text="transaction"),)
    assert opened.operations == (
        MutationOperation(kind="open", account_name="Assets:Cash:Wallet", text="open directive"),
    )
    assert updated.operations == (
        MutationOperation(
            kind="replace",
            target_file="data/agent_inc/current.beancount",
            old_text="old",
            text="new",
        ),
    )
    assert bulk.operations == (MutationOperation(kind="append", text="transactions"),)
    assert "replacement" in updated.remediation
    assert "batch" in bulk.remediation


def test_change_set_and_reconciliation_plans_preserve_order_and_default_messages() -> None:
    operations = [
        MutationOperation(kind="open", account_name="Assets:Cash:Wallet", text="open"),
        MutationOperation(kind="append", text="transaction"),
    ]

    change_set = MutationPlanner.change_set(operations, "record wallet transaction")
    reconciliation = MutationPlanner.reconciliation("adjustment", "")
    checkpoint = MutationPlanner.reconciliation("adjustment", "", checkpoint_update=True)

    assert change_set.operations == tuple(operations)
    assert change_set.commit_message == "record wallet transaction"
    assert reconciliation.commit_message == "chore(ledger): reconcile balance"
    assert checkpoint.commit_message == "chore(ledger): update balance checkpoint"


def test_prepare_commit_persists_the_canonical_sealed_plan(ledger_workspace: Path) -> None:
    transaction = (
        '2026-06-15 * "Dinner"\n  Expenses:Food:Dining  100 CNY\n  Assets:Cash          -100 CNY'
    )
    message = "record dinner"

    pending = LedgerService().prepare_commit(str(ledger_workspace), transaction, message)

    assert isinstance(pending, PendingAction)
    prepared = (
        MutationPreparationHandlerRegistry()
        .get("commit_transaction")
        .build(str(ledger_workspace), transaction_text=transaction, commit_message=message)
    )
    assert isinstance(prepared, PreparedMutation)
    expected = MutationCoordinator.seal(str(ledger_workspace), prepared.plan).to_spec()
    assert pending.execution_spec["mutation_plan"] == expected
