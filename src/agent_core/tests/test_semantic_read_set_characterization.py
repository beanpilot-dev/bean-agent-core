"""Characterize semantic inputs that a sealed mutation plan must protect.

These tests deliberately describe the v2-plan stale-read contract.  The
current v1 plans only fingerprint writable sidecar paths, so the first three
tests are expected to fail until semantic facts are sealed and rechecked.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_core.services.beancount import Beancount
from agent_core.services.ledger import LedgerService
from agent_core.services.types import ApplyReceipt, InvariantViolation, PendingAction

DINNER = (
    '2026-06-15 * "Dinner"\n'
    "  Expenses:Food:Dining  100 CNY\n"
    "  Assets:Cash          -100 CNY"
)


def _publisher() -> Mock:
    publisher = Mock()
    publisher.commit_and_push.return_value = {"ok": True, "error": None, "push": "PUSHED"}
    return publisher


def _add_primary_include(workspace: Path, filename: str) -> Path:
    """Add a non-sidecar source file whose digest v1 plans do not seal."""
    main = workspace / "data" / "main.beancount"
    main.write_text(main.read_text() + f'include "{filename}"\n')
    included = workspace / "data" / filename
    included.write_text("")
    return included


def _assert_stale(result: object, publisher: Mock) -> None:
    assert isinstance(result, InvariantViolation)
    assert result.invariant == "MUTATION_PRECONDITION_FAILED"
    publisher.commit_and_push.assert_not_called()


def test_commit_rejects_account_lifecycle_change_in_included_source(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A close directive used by account policy must invalidate the approval."""
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    included = _add_primary_include(ledger_workspace, "account-lifecycle.beancount")
    pending = LedgerService().prepare_commit(str(ledger_workspace), DINNER, "record dinner")
    assert isinstance(pending, PendingAction)

    # This is an input to account lifecycle policy, but not a mutation target.
    included.write_text("2026-06-14 close Expenses:Food:Dining\n")
    publisher = _publisher()
    result = LedgerService().apply_pending_action(
        str(ledger_workspace), pending.__dict__.copy(), "repo", publisher
    )

    _assert_stale(result, publisher)


def test_commit_rejects_checkpoint_added_in_included_source(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transaction approval cannot cross a changed relevant balance checkpoint."""
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    included = _add_primary_include(ledger_workspace, "checkpoints.beancount")
    pending = LedgerService().prepare_commit(str(ledger_workspace), DINNER, "record dinner")
    assert isinstance(pending, PendingAction)

    # The assertion is valid before the proposed 2026-06-15 transaction, so
    # bean-check alone cannot reliably stand in for stale-plan detection.
    included.write_text("2026-06-01 balance Assets:Cash  9915 CNY\n")
    publisher = _publisher()
    result = LedgerService().apply_pending_action(
        str(ledger_workspace), pending.__dict__.copy(), "repo", publisher
    )

    _assert_stale(result, publisher)


def test_reconciliation_rejects_changed_balance_input_in_included_source(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed historical transaction must invalidate a calculated adjustment."""
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    included = _add_primary_include(ledger_workspace, "late-import.beancount")
    pending = LedgerService().prepare_balance_reconciliation(
        str(ledger_workspace),
        "2026-05-31",
        "Assets:Bank:Checking",
        "5120",
        "CNY",
        "Equity:Opening-Balances",
    )
    assert isinstance(pending, PendingAction)

    included.write_text(
        '2026-05-30 * "Late bank import"\n'
        "  Assets:Bank:Checking          10 CNY\n"
        "  Equity:Opening-Balances       -10 CNY\n"
    )
    publisher = _publisher()
    result = LedgerService().apply_pending_action(
        str(ledger_workspace), pending.__dict__.copy(), "repo", publisher
    )

    _assert_stale(result, publisher)


def test_commit_allows_unrelated_non_ledger_file_change(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narrow read-set protection should not reject unrelated repository edits."""
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    pending = LedgerService().prepare_commit(str(ledger_workspace), DINNER, "record dinner")
    assert isinstance(pending, PendingAction)

    (ledger_workspace / "README.md").write_text("documentation-only change\n")
    publisher = _publisher()
    result = LedgerService().apply_pending_action(
        str(ledger_workspace), pending.__dict__.copy(), "repo", publisher
    )

    assert isinstance(result, ApplyReceipt)
    target = ledger_workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
    assert "Dinner" in target.read_text()
    publisher.commit_and_push.assert_called_once()
