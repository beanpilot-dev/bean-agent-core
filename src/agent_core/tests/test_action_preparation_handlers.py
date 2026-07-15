"""Parity coverage for the first registered preparation-handler migration."""

from pathlib import Path

import pytest

from agent_core.services.ledger import LedgerService
from agent_core.services.mutations.handlers import (
    MutationPreparationHandlerRegistry,
    PreparedMutation,
)
from agent_core.services.types import PendingAction, Preview

TRANSACTION = (
    '2026-06-15 * "Dinner"\n  Expenses:Food:Dining  100 CNY\n  Assets:Cash          -100 CNY'
)


@pytest.mark.parametrize(
    ("action_type", "kwargs", "expected_diff"),
    [
        (
            "commit_transaction",
            {
                "transaction_text": TRANSACTION,
                "commit_message": "record dinner",
                "whitelist": ["Expenses:Food", "Assets:Cash"],
            },
            TRANSACTION,
        ),
        (
            "open_account",
            {
                "account_name": "Assets:Bank:Savings",
                "currency": "CNY",
                "open_date": "2026-06-15",
                "display_name": "Savings",
            },
            '2026-06-15 open Assets:Bank:Savings  CNY\n  name: "Savings"',
        ),
    ],
)
def test_registered_handler_preserves_preview_and_pending_contract(
    ledger_workspace: Path,
    action_type: str,
    kwargs: dict[str, object],
    expected_diff: str,
) -> None:
    """The façade has the same action-owned preview and pending payload facts."""
    handler = MutationPreparationHandlerRegistry().get(action_type)
    expected = handler.build(str(ledger_workspace), **kwargs)
    assert isinstance(expected, PreparedMutation)
    assert not hasattr(expected, "preview")
    assert not hasattr(expected, "pending_action")

    service = LedgerService()
    if action_type == "commit_transaction":
        preview = service.preview_commit(str(ledger_workspace), **kwargs)
        pending = service.prepare_commit(str(ledger_workspace), **kwargs)
    else:
        preview = service.preview_open(str(ledger_workspace), **kwargs)
        pending = service.prepare_open(str(ledger_workspace), **kwargs)

    assert isinstance(preview, Preview)
    assert isinstance(pending, PendingAction)
    assert preview.operation == expected.action_type
    assert preview.preview["validation"]["status"] == "validated"
    assert pending.action_type == expected.action_type
    assert pending.display["diff"] == expected_diff
    assert pending.display["preview"] == preview.preview
    assert pending.execution_spec["mutation_plan"]["operations"] == [
        operation.to_spec() for operation in expected.plan.operations
    ]
    assert all(fact.kind == "account_state" for fact in expected.plan.semantic_facts)
    for key, value in expected.execution_spec.items():
        assert pending.execution_spec[key] == value
