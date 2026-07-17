"""Focused parity coverage for update and bulk preparation handlers."""

from datetime import date
from pathlib import Path

from agent_core.services.ledger import LedgerService
from agent_core.services.mutations.handlers import (
    BulkCommitPreparationHandler,
    MutationPreparationHandlerRegistry,
    PreparedMutation,
    TransactionUpdatePreparationHandler,
)
from agent_core.services.transaction_locator import TransactionLocator
from agent_core.services.types import InvariantViolation, LedgerConfig, PendingAction, Preview

REPLACEMENT = (
    '2026-05-12 * "Lunch"\n'
    "  Expenses:Food:Dining  95 CNY\n"
    "  Assets:Cash          -95 CNY"
)
BULK = (
    '2026-06-15 * "Dinner"\n'
    "  Expenses:Food:Dining  100 CNY\n"
    "  Assets:Cash          -100 CNY"
)


def _custom_workspace(tmp_path: Path) -> tuple[Path, LedgerConfig]:
    sidecar = tmp_path / "books" / "agent_sidecar"
    sidecar.mkdir(parents=True)
    month = date.today().strftime("%Y-%m")
    config = LedgerConfig(
        entry_path="books/root.beancount",
        sidecar_main_path="books/agent_sidecar/main.beancount",
        sidecar_write_dir="books/agent_sidecar",
    )
    (tmp_path / "books" / "root.beancount").write_text(
        'option "operating_currency" "CNY"\n'
        'include "agent_sidecar/main.beancount"\n'
    )
    (sidecar / "main.beancount").write_text(
        "2020-01-01 open Assets:Cash CNY\n"
        "2020-01-01 open Expenses:Food:Dining CNY\n"
        f'include "{month}.beancount"\n'
    )
    (sidecar / f"{month}.beancount").write_text(
        '2026-05-12 * "Lunch"\n'
        "  Expenses:Food:Dining  85 CNY\n"
        "  Assets:Cash          -85 CNY\n"
    )
    return tmp_path, config


def test_registry_exposes_all_preparation_keys() -> None:
    registry = MutationPreparationHandlerRegistry()

    assert registry.keys() == (
        "commit_transaction",
        "open_account",
        "update_transaction",
        "bulk_commit",
        "change_set",
        "balance_reconciliation",
        "balance_update",
    )


def test_update_handler_owns_lookup_policy_plan_and_presentation(
    ledger_workspace: Path,
) -> None:
    before = {
        path: path.read_text()
        for path in (ledger_workspace / "data").rglob("*.beancount")
    }

    prepared = TransactionUpdatePreparationHandler().build(
        str(ledger_workspace),
        target_date="2026-05-12",
        narration="Lunch",
        new_transaction_text=REPLACEMENT,
        commit_message="update lunch",
    )

    assert isinstance(prepared, PreparedMutation)
    assert prepared.action_type == "update_transaction"
    assert prepared.preview_fields["file"].startswith("data/agent_inc/")
    assert prepared.preview_fields["advisory"]["warning"] == "VALUE_CHANGED"
    assert prepared.display_fields == {
        "kind": "transaction_update_preview",
        "summary": "Update a transaction",
        "diff": REPLACEMENT,
    }
    operation = prepared.plan.operations[0]
    assert operation.kind == "replace"
    assert operation.target_file == prepared.preview_fields["file"]
    assert operation.old_text == prepared.preview_fields["found_block"]
    assert operation.text == REPLACEMENT
    assert {fact.subject for fact in prepared.plan.semantic_facts} == {
        "Assets:Cash",
        "Expenses:Food:Dining",
    }
    assert {
        path: path.read_text()
        for path in (ledger_workspace / "data").rglob("*.beancount")
    } == before


def test_update_shared_materialization_preserves_preview_and_pending_contract(
    ledger_workspace: Path,
) -> None:
    service = LedgerService()

    preview = service.preview_update(
        str(ledger_workspace), "2026-05-12", "Lunch", REPLACEMENT, "update lunch"
    )
    pending = service.prepare_update(
        str(ledger_workspace), "2026-05-12", "Lunch", REPLACEMENT, "update lunch"
    )

    assert isinstance(preview, Preview)
    assert isinstance(pending, PendingAction)
    assert preview.operation == "update_transaction"
    assert set(preview.preview) == {
        "found_block",
        "replacement",
        "file",
        "commit_message",
        "advisory",
        "validation",
    }
    assert pending.execution_spec["target_date"] == "2026-05-12"
    assert pending.execution_spec["new_transaction_text"] == REPLACEMENT
    assert pending.display == {
        "kind": "transaction_update_preview",
        "summary": "Update a transaction",
        "diff": REPLACEMENT,
        "preview": pending.display["preview"],
    }
    assert pending.display["preview"] == preview.preview
    assert pending.validation["file"] == preview.preview["file"]
    assert pending.validation["advisory"] == preview.preview["advisory"]
    assert pending.validation["dry_run"]["status"] == "validated"
    assert pending.policy["risk"] == "elevated"


def test_transaction_locator_and_update_policy_report_ambiguous_matches(
    ledger_workspace: Path,
) -> None:
    duplicate = ledger_workspace / "duplicate.beancount"
    duplicate.write_text(REPLACEMENT)
    sidecar_main = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    sidecar_main.write_text(sidecar_main.read_text() + '\ninclude "../../duplicate.beancount"\n')

    matches = TransactionLocator.find(str(ledger_workspace), "2026-05-12", "Lunch")
    result = TransactionUpdatePreparationHandler().build(
        str(ledger_workspace),
        target_date="2026-05-12",
        narration="Lunch",
        new_transaction_text=REPLACEMENT,
        commit_message="update lunch",
    )

    assert len(matches) == 2
    assert isinstance(result, InvariantViolation)
    assert result.invariant == "AMBIGUOUS_MATCH"
    assert len(result.detail["matches_found"]) == 2


def test_update_handler_rejects_user_authored_transaction_target(
    ledger_workspace: Path,
) -> None:
    legacy = ledger_workspace / "data" / "legacy.beancount"
    legacy.write_text(
        '2026-05-13 * "Legacy lunch"\n'
        "  Expenses:Food:Dining  85 CNY\n"
        "  Assets:Cash          -85 CNY\n"
    )
    entry = ledger_workspace / "data" / "main.beancount"
    entry.write_text(entry.read_text() + 'include "legacy.beancount"\n')

    result = TransactionUpdatePreparationHandler().build(
        str(ledger_workspace),
        target_date="2026-05-13",
        narration="Legacy lunch",
        new_transaction_text=REPLACEMENT.replace("2026-05-12", "2026-05-13"),
        commit_message="update legacy lunch",
    )

    assert isinstance(result, InvariantViolation)
    assert result.invariant == "SIDECAR_WRITE_ISOLATION"
    assert result.provided == {"file": "data/legacy.beancount"}


def test_bulk_handler_resolves_staging_once_and_preserves_payload(
    ledger_workspace: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "staged.beancount"
    staging.write_text(BULK)

    prepared = BulkCommitPreparationHandler().build(
        str(ledger_workspace),
        transactions_file=str(staging),
        commit_message="bulk",
    )
    pending = LedgerService().prepare_bulk(
        str(ledger_workspace),
        commit_message="bulk",
        transactions_file=str(staging),
    )

    assert isinstance(prepared, PreparedMutation)
    assert isinstance(pending, PendingAction)
    assert prepared.preview_fields == {
        "transaction_count": 1,
        "sample": '2026-06-15 * "Dinner"',
        "commit_message": "bulk",
    }
    assert prepared.plan.operations[0].text == BULK
    assert {fact.subject for fact in prepared.plan.semantic_facts} == {
        "Assets:Cash",
        "Expenses:Food:Dining",
    }
    assert pending.execution_spec["transactions_text"] == BULK
    assert pending.display["diff"] == '2026-06-15 * "Dinner"'
    assert pending.validation["transaction_count"] == 1
    assert pending.validation["target_file"].startswith("data/agent_inc/")
    assert staging.exists()


def test_bulk_handler_uses_custom_ledger_layout_and_reports_input_errors(
    tmp_path: Path,
) -> None:
    workspace, config = _custom_workspace(tmp_path)
    service = LedgerService()

    pending = service.prepare_bulk(str(workspace), BULK, "bulk", ledger_config=config)
    missing = service.preview_bulk(str(workspace), ledger_config=config)
    staging_error = service.preview_bulk(
        str(workspace), transactions_file=str(tmp_path / "missing.beancount"), ledger_config=config
    )

    assert isinstance(pending, PendingAction)
    assert pending.validation["target_file"].startswith("books/agent_sidecar/")
    assert isinstance(missing, InvariantViolation)
    assert missing.invariant == "MISSING_INPUT"
    assert isinstance(staging_error, InvariantViolation)
    assert staging_error.invariant == "STAGING_ERROR"
