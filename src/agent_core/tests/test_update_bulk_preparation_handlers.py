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
from agent_core.services.queries import LedgerQueryService
from agent_core.services.transaction_index import mint_transaction_ref
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


def _detail(ledger_workspace: Path, narration: str = "Lunch") -> dict:
    found = LedgerQueryService.find_transactions(
        str(ledger_workspace), narration_contains=narration
    )
    return LedgerQueryService.get_transaction(
        str(ledger_workspace), found.rows[0]["transaction_ref"]
    ).transaction or {}


def test_registry_exposes_all_preparation_keys() -> None:
    registry = MutationPreparationHandlerRegistry()

    assert registry.keys() == (
        "commit_transaction",
        "open_account",
        "close_account",
        "update_transaction",
        "delete_transaction",
        "price",
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

    detail = _detail(ledger_workspace)
    prepared = TransactionUpdatePreparationHandler().build(
        str(ledger_workspace),
        transaction_ref=detail["transaction_ref"],
        revision_fingerprint=detail["revision_fingerprint"],
        new_transaction_text=REPLACEMENT,
        commit_message="update lunch",
    )

    assert isinstance(prepared, PreparedMutation)
    assert prepared.action_type == "update_transaction"
    assert prepared.preview_fields["source_path"].startswith("data/agent_inc/")
    assert prepared.preview_fields["advisory"]["warning"] == "VALUE_CHANGED"
    assert prepared.display_fields == {
        "kind": "transaction_update_preview",
        "summary": "Update a transaction",
        "transaction_ref": detail["transaction_ref"],
        "revision_fingerprint": detail["revision_fingerprint"],
        "source_path": detail["source_path"],
        "source_start_line": detail["source_start_line"],
        "source_end_line": detail["source_end_line"],
        "old_directive": detail["directive"],
        "new_directive": REPLACEMENT,
        "diff": REPLACEMENT,
    }
    operation = prepared.plan.operations[0]
    assert operation.kind == "replace"
    assert operation.target_file == prepared.preview_fields["source_path"]
    assert operation.old_text == prepared.preview_fields["old_directive"]
    assert operation.text == REPLACEMENT
    assert {fact.subject for fact in prepared.plan.semantic_facts} == {
        detail["transaction_ref"],
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

    detail = _detail(ledger_workspace)
    preview = service.preview_transaction_update(
        str(ledger_workspace),
        detail["transaction_ref"],
        detail["revision_fingerprint"],
        REPLACEMENT,
        "update lunch",
    )
    pending = service.prepare_transaction_update(
        str(ledger_workspace),
        detail["transaction_ref"],
        detail["revision_fingerprint"],
        REPLACEMENT,
        "update lunch",
    )

    assert isinstance(preview, Preview)
    assert isinstance(pending, PendingAction)
    assert preview.operation == "update_transaction"
    assert set(preview.preview) == {
        "transaction_ref",
        "revision_fingerprint",
        "old_directive",
        "new_directive",
        "source_path",
        "source_start_line",
        "source_end_line",
        "commit_message",
        "advisory",
        "validation",
    }
    assert pending.execution_spec["transaction_ref"] == detail["transaction_ref"]
    assert pending.execution_spec["revision_fingerprint"] == detail["revision_fingerprint"]
    assert pending.execution_spec["new_transaction_text"] == REPLACEMENT
    assert pending.display == {
        "kind": "transaction_update_preview",
        "summary": "Update a transaction",
        "transaction_ref": detail["transaction_ref"],
        "revision_fingerprint": detail["revision_fingerprint"],
        "source_path": detail["source_path"],
        "source_start_line": detail["source_start_line"],
        "source_end_line": detail["source_end_line"],
        "old_directive": detail["directive"],
        "new_directive": REPLACEMENT,
        "diff": REPLACEMENT,
        "preview": pending.display["preview"],
    }
    assert pending.display["preview"] == preview.preview
    assert pending.validation["file"] == preview.preview["source_path"]
    assert pending.validation["advisory"] == preview.preview["advisory"]
    assert pending.validation["dry_run"]["status"] == "validated"
    assert pending.policy["risk"] == "elevated"


def test_duplicate_narrations_are_selected_by_reference(
    ledger_workspace: Path,
) -> None:
    duplicate = ledger_workspace / "duplicate.beancount"
    duplicate.write_text(REPLACEMENT)
    sidecar_main = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    sidecar_main.write_text(sidecar_main.read_text() + '\ninclude "../../duplicate.beancount"\n')

    matches = LedgerQueryService.find_transactions(
        str(ledger_workspace), narration_contains="Lunch"
    )
    detail = LedgerQueryService.get_transaction(
        str(ledger_workspace), matches.rows[0]["transaction_ref"]
    ).transaction or {}
    result = TransactionUpdatePreparationHandler().build(
        str(ledger_workspace),
        transaction_ref=detail["transaction_ref"],
        revision_fingerprint=detail["revision_fingerprint"],
        new_transaction_text=REPLACEMENT,
        commit_message="update lunch",
    )

    assert matches.total == 2
    assert isinstance(result, PreparedMutation)


def test_update_handler_rejects_stale_revision_fingerprint(ledger_workspace: Path) -> None:
    detail = _detail(ledger_workspace)
    result = TransactionUpdatePreparationHandler().build(
        str(ledger_workspace),
        transaction_ref=detail["transaction_ref"],
        revision_fingerprint="sha256:" + "0" * 64,
        new_transaction_text=REPLACEMENT,
        commit_message="update lunch",
    )

    assert isinstance(result, InvariantViolation)
    assert result.invariant == "STALE_TRANSACTION_REVISION"
    assert "ledger_get_transaction" in result.remediation


def test_update_handler_rejects_forged_or_missing_reference(ledger_workspace: Path) -> None:
    malformed = TransactionUpdatePreparationHandler().build(
        str(ledger_workspace),
        transaction_ref="txn_v1_forged",
        revision_fingerprint="sha256:" + "0" * 64,
        new_transaction_text=REPLACEMENT,
        commit_message="update lunch",
    )
    missing = TransactionUpdatePreparationHandler().build(
        str(ledger_workspace),
        transaction_ref=mint_transaction_ref(
            {
                "version": 1,
                "path": "data/agent_inc/missing.beancount",
                "start_line": 1,
                "occurrence": 1,
                "directive_identity": "0" * 64,
            }
        ),
        revision_fingerprint="sha256:" + "0" * 64,
        new_transaction_text=REPLACEMENT,
        commit_message="update lunch",
    )

    assert isinstance(malformed, InvariantViolation)
    assert malformed.invariant == "MALFORMED_TRANSACTION_REF"
    assert isinstance(missing, InvariantViolation)
    assert missing.invariant == "TRANSACTION_NOT_FOUND"


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

    detail = _detail(ledger_workspace, "Legacy lunch")
    result = TransactionUpdatePreparationHandler().build(
        str(ledger_workspace),
        transaction_ref=detail["transaction_ref"],
        revision_fingerprint=detail["revision_fingerprint"],
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
