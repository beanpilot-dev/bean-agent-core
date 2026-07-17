from datetime import date
from pathlib import Path
from unittest.mock import Mock

from agent_core.services.ledger import LedgerService
from agent_core.services.mutations import MutationPlan
from agent_core.services.pending_actions import digest_payload
from agent_core.services.transaction_index import TransactionIndex
from agent_core.services.types import InvariantViolation, PendingAction


def _approval_proof(action: PendingAction) -> dict[str, str]:
    payload = action.__dict__.copy()
    return {
        "approved_by": "user_123",
        "approved_at": "2026-07-17T00:00:00Z",
        "approval_id": "approval_123",
        "pending_action_id": action.pending_action_id,
        "payload_digest": digest_payload(payload),
        "integrity_digest": action.digest,
        "host": "test-host",
    }


def _lunch_transaction() -> str:
    return (
        '2026-05-12 * "Lunch"\n'
        "  Expenses:Food:Dining  85 CNY\n"
        "  Assets:Cash          -85 CNY\n"
    )


def test_delete_prepares_full_high_risk_preview_and_applies_exact_directive(
    ledger_workspace: Path,
    monkeypatch,
) -> None:
    index = TransactionIndex.build(str(ledger_workspace))
    transaction = index.search(narration_contains="Lunch")[0]
    service = LedgerService()

    pending = service.prepare_transaction_delete(
        str(ledger_workspace),
        transaction.transaction_ref,
        transaction.revision_fingerprint,
        "delete duplicate lunch",
    )

    assert isinstance(pending, PendingAction)
    assert pending.action_type == "delete_transaction"
    assert pending.policy["risk"] == "high"
    assert pending.policy["requires_elevated_review"] is True
    assert pending.policy["reasons"] == ["transaction_deletion"]
    assert pending.display["kind"] == "transaction_delete_preview"
    assert pending.display["removed_directive"] == _lunch_transaction()
    assert "new_directive" not in pending.display
    assert pending.execution_spec["mutation_plan"]["operations"][0]["kind"] == "delete"

    monkeypatch.setattr("agent_core.services.beancount.Beancount.bean_format", lambda *_args: None)
    publisher = Mock()
    publisher.commit_and_push.return_value = {
        "ok": True,
        "error": None,
        "push": "PUSHED: ok",
        "commit_sha": "abc123def4567890",
    }
    applied = service.apply_pending_action(
        str(ledger_workspace),
        pending.__dict__.copy(),
        "repo",
        publisher,
        approval_proof=_approval_proof(pending),
    )

    assert applied.status == "APPLIED"
    assert applied.receipt["commit_sha"] == "abc123def4567890"
    assert applied.receipt["result"]["audit"]["deletion_classification"] == (
        "high_risk_transaction_deletion"
    )
    month_file = ledger_workspace / f"data/agent_inc/{date.today():%Y-%m}.beancount"
    assert "Lunch" not in month_file.read_text()
    publisher.commit_and_push.assert_called_once()


def test_delete_uses_reference_line_when_identical_directives_collide(
    ledger_workspace: Path,
    monkeypatch,
) -> None:
    path = ledger_workspace / f"data/agent_inc/{date.today():%Y-%m}.beancount"
    path.write_text(path.read_text() + "\n" + _lunch_transaction())
    transactions = TransactionIndex.build(str(ledger_workspace)).search(narration_contains="Lunch")
    assert len(transactions) == 2
    target = transactions[1]

    pending = LedgerService().prepare_transaction_delete(
        str(ledger_workspace),
        target.transaction_ref,
        target.revision_fingerprint,
        "delete one duplicate lunch",
    )
    assert isinstance(pending, PendingAction)
    monkeypatch.setattr("agent_core.services.beancount.Beancount.bean_format", lambda *_args: None)
    publisher = Mock()
    publisher.commit_and_push.return_value = {"ok": True, "push": "PUSHED: ok", "commit_sha": "sha"}

    result = LedgerService().apply_pending_action(
        str(ledger_workspace),
        pending.__dict__.copy(),
        "repo",
        publisher,
        approval_proof=_approval_proof(pending),
    )

    assert result.status == "APPLIED"
    assert path.read_text().count('2026-05-12 * "Lunch"') == 1


def test_delete_requires_explicit_elevated_approval_proof(
    ledger_workspace: Path,
) -> None:
    transaction = TransactionIndex.build(str(ledger_workspace)).search(
        narration_contains="Lunch"
    )[0]
    pending = LedgerService().prepare_transaction_delete(
        str(ledger_workspace),
        transaction.transaction_ref,
        transaction.revision_fingerprint,
        "delete lunch",
    )
    assert isinstance(pending, PendingAction)
    publisher = Mock()

    rejected = LedgerService().apply_pending_action(
        str(ledger_workspace), pending.__dict__.copy(), "repo", publisher
    )

    assert rejected.status == "INTEGRITY_FAILED"
    assert "elevated approval proof" in rejected.error
    publisher.commit_and_push.assert_not_called()


def test_delete_rejects_stale_fingerprint_and_non_sidecar_target(ledger_workspace: Path) -> None:
    index = TransactionIndex.build(str(ledger_workspace))
    transaction = index.search(narration_contains="Lunch")[0]
    stale = LedgerService().prepare_transaction_delete(
        str(ledger_workspace),
        transaction.transaction_ref,
        "sha256:stale",
        "delete lunch",
    )
    assert isinstance(stale, InvariantViolation)
    assert stale.invariant == "STALE_TRANSACTION_REVISION"

    legacy = ledger_workspace / "data/main.beancount"
    legacy.write_text(
        legacy.read_text()
        + '\n2026-05-13 * "Legacy"\n  Expenses:Food:Dining  1 CNY\n  Assets:Cash -1 CNY\n'
    )
    legacy_txn = TransactionIndex.build(str(ledger_workspace)).search(
        narration_contains="Legacy"
    )[0]
    rejected = LedgerService().prepare_transaction_delete(
        str(ledger_workspace),
        legacy_txn.transaction_ref,
        legacy_txn.revision_fingerprint,
        "delete legacy",
    )
    assert isinstance(rejected, InvariantViolation)
    assert rejected.invariant == "SIDECAR_WRITE_ISOLATION"


def test_old_plan_versions_decode_and_delete_requires_current_version() -> None:
    operation = {
        "kind": "replace",
        "text": "new",
        "target_file": "data/agent_inc/main.beancount",
        "account_name": None,
        "old_text": "old",
    }
    for version in (1, 2):
        decoded = MutationPlan.from_spec(
            {
                "version": version,
                "operations": [operation],
                "commit_message": "old plan",
                "remediation": "retry",
                "preconditions": [],
            }
        )
        assert decoded.schema_version == version
        assert decoded.operations[0].kind == "replace"

    try:
        MutationPlan.from_spec(
            {
                "version": 2,
                "operations": [{**operation, "kind": "delete"}],
                "commit_message": "invalid old delete",
                "remediation": "retry",
                "preconditions": [],
                "semantic_facts": [],
            }
        )
    except ValueError as error:
        assert "current mutation plan version" in str(error)
    else:  # pragma: no cover
        raise AssertionError("legacy plans must not smuggle in delete operations")
