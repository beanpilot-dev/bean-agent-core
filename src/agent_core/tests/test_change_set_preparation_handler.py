"""Focused policy and payload coverage for composite change-set preparation."""

from datetime import date
from pathlib import Path

from agent_core.services.approvals.contracts import PendingActionService
from agent_core.services.mutations.handlers.change_set import ChangeSetPreparationHandler
from agent_core.services.mutations.handlers.contracts import PreparedMutation
from agent_core.services.types import InvariantViolation

DEPENDENT_TRANSACTION = (
    '2026-06-16 * "Savings transfer"\n'
    "  Assets:Bank:Savings   100 CNY\n"
    "  Assets:Cash          -100 CNY"
)


def _dependent_operations() -> list[dict[str, object]]:
    return [
        {
            "type": "open_account",
            "account_name": "Assets:Bank:Savings",
            "currency": "CNY",
            "open_date": "2026-06-16",
            "display_name": "Savings",
        },
        {
            "type": "commit_transaction",
            "transaction_text": DEPENDENT_TRANSACTION,
        },
    ]


def test_change_set_handler_preserves_order_payload_and_read_set(
    ledger_workspace: Path,
) -> None:
    operations = _dependent_operations()
    before = {path: path.read_text() for path in (ledger_workspace / "data").rglob("*.beancount")}

    prepared = ChangeSetPreparationHandler().build(
        str(ledger_workspace),
        operations=operations,
        commit_message="record savings transfer",
    )

    assert isinstance(prepared, PreparedMutation)
    assert prepared.handler_key == "change_set"
    assert prepared.action_type == "change_set"
    assert prepared.preview_fields == {}
    assert prepared.embed_preview_in_display is False
    assert prepared.execution_spec == {
        "operations": operations,
        "commit_message": "record savings transfer",
    }
    assert [operation.kind for operation in prepared.plan.operations] == ["open", "append"]
    assert prepared.plan.operations[0].account_name == "Assets:Bank:Savings"
    assert prepared.plan.operations[0].text == (
        '2026-06-16 open Assets:Bank:Savings  CNY\n  name: "Savings"'
    )
    assert prepared.plan.operations[1].text == DEPENDENT_TRANSACTION
    assert prepared.plan.commit_message == "record savings transfer"
    assert [(fact.kind, fact.subject) for fact in prepared.plan.semantic_facts] == [
        ("account_state", "Assets:Bank:Savings"),
        ("account_state", "Assets:Cash"),
    ]
    assert prepared.validation_fields == {
        "operation_count": 2,
        "transaction_count": 1,
        "accounts": ["Assets:Bank:Savings", "Assets:Cash"],
        "target_files": [
            "data/agent_inc/main.beancount",
            f"data/agent_inc/{date.today():%Y-%m}.beancount",
        ],
    }
    assert prepared.display_fields == {
        "kind": "change_set_preview",
        "summary": "Apply 2 related ledger changes",
        "diff": (
            '2026-06-16 open Assets:Bank:Savings  CNY\n  name: "Savings"\n\n'
            f"{DEPENDENT_TRANSACTION}"
        ),
        "items": [
            {
                "operation_index": 0,
                "type": "open_account",
                "summary": "Open Assets:Bank:Savings",
                "diff": ('2026-06-16 open Assets:Bank:Savings  CNY\n  name: "Savings"'),
                "target_file": "data/agent_inc/main.beancount",
            },
            {
                "operation_index": 1,
                "type": "commit_transaction",
                "summary": "Record a transaction",
                "diff": DEPENDENT_TRANSACTION,
                "target_file": f"data/agent_inc/{date.today():%Y-%m}.beancount",
                "accounts": ["Assets:Bank:Savings", "Assets:Cash"],
            },
        ],
    }
    assert {
        path: path.read_text() for path in (ledger_workspace / "data").rglob("*.beancount")
    } == before


def test_change_set_handler_rejects_dependency_before_account_open(
    ledger_workspace: Path,
) -> None:
    result = ChangeSetPreparationHandler().build(
        str(ledger_workspace),
        operations=[
            {"type": "commit_transaction", "transaction_text": DEPENDENT_TRANSACTION},
            _dependent_operations()[0],
        ],
        commit_message="record savings transfer",
    )

    assert isinstance(result, InvariantViolation)
    assert result.invariant == "ACCOUNT_WHITELIST"
    assert result.provided == ["Assets:Bank:Savings"]
    assert result.detail["operation_index"] == 0
    assert "Assets:Cash" in result.detail["valid_accounts"]


def test_change_set_handler_reports_indexed_input_policy_errors(
    ledger_workspace: Path,
) -> None:
    handler = ChangeSetPreparationHandler()

    missing = handler.build(str(ledger_workspace), operations=[], commit_message="missing")
    invalid_name = handler.build(
        str(ledger_workspace),
        operations=[
            {"operation": "open_account", "account_name": "Savings", "open_date": "2026-06-16"}
        ],
        commit_message="invalid",
    )
    unsupported = handler.build(
        str(ledger_workspace),
        operations=[{"type": "update_transaction"}],
        commit_message="unsupported",
    )

    assert isinstance(missing, InvariantViolation)
    assert missing.invariant == "MISSING_OPERATIONS"
    assert isinstance(invalid_name, InvariantViolation)
    assert invalid_name.invariant == "ACCOUNT_NAME_FORMAT"
    assert invalid_name.detail["operation_index"] == 0
    assert isinstance(unsupported, InvariantViolation)
    assert unsupported.invariant == "UNSUPPORTED_CHANGE_SET_OPERATION"
    assert unsupported.provided == "update_transaction"
    assert unsupported.detail["operation_index"] == 0


def test_change_set_handler_applies_conversation_scope_to_ordered_transactions(
    ledger_workspace: Path,
) -> None:
    result = ChangeSetPreparationHandler().build(
        str(ledger_workspace),
        operations=_dependent_operations(),
        commit_message="record savings transfer",
        whitelist=["Assets:Bank"],
    )

    assert isinstance(result, InvariantViolation)
    assert result.invariant == "CONVERSATION_SCOPE"
    assert result.provided == ["Assets:Cash"]
    assert result.detail == {
        "operation_index": 1,
        "allowed_prefixes": ["Assets:Bank"],
    }


def test_change_set_handler_preserves_transaction_count_risk_fact(
    ledger_workspace: Path,
) -> None:
    operations = [
        {
            "type": "commit_transaction",
            "transaction_text": (
                f'2026-06-{day:02d} * "Small purchase {day}"\n'
                "  Expenses:Food:Dining    1 CNY\n"
                "  Assets:Cash            -1 CNY"
            ),
        }
        for day in range(1, 26)
    ]
    prepared = ChangeSetPreparationHandler().build(
        str(ledger_workspace),
        operations=operations,
        commit_message="record many small purchases",
    )

    assert isinstance(prepared, PreparedMutation)
    assert prepared.validation_fields["transaction_count"] == 25
    pending = PendingActionService.create_pending_action(
        action_type=prepared.action_type,
        execution_spec=prepared.execution_spec,
        display=prepared.display_fields,
        validation={"status": "validated", **prepared.validation_fields},
    )
    assert pending.policy["risk"] == "high"
    assert pending.policy["requires_elevated_review"] is True
