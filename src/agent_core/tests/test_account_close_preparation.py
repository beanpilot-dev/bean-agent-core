from datetime import date
from pathlib import Path
from unittest.mock import Mock

from agent_core.services.ledger import LedgerService
from agent_core.services.mutations.handlers.account_close import AccountClosePreparationHandler
from agent_core.services.pending_actions import digest_payload
from agent_core.services.types import InvariantViolation, PendingAction


def test_account_close_prepares_zero_balance_sidecar_action(
    ledger_workspace: Path,
) -> None:
    before = {
        path: path.read_text()
        for path in (ledger_workspace / "data").rglob("*.beancount")
    }
    pending = LedgerService().prepare_account_close(
        str(ledger_workspace), "Income:Salary", "2026-07-17", "close salary"
    )

    assert isinstance(pending, PendingAction)
    assert pending.action_type == "close_account"
    assert pending.policy["risk"] == "high"
    assert pending.policy["reasons"] == ["account_closure"]
    assert pending.display["kind"] == "account_close_preview"
    assert pending.display["directive"] == "2026-07-17 close Income:Salary"
    assert pending.display["balance_status"] == "zero"
    assert pending.display["open_date"] == "2020-01-01"
    assert pending.display["target_file"] == "data/agent_inc/" + f"{date.today():%Y-%m}.beancount"
    assert {
        path: path.read_text()
        for path in (ledger_workspace / "data").rglob("*.beancount")
    } == before


def test_account_close_rejects_nonzero_and_future_postings(
    ledger_workspace: Path,
) -> None:
    nonzero = LedgerService().prepare_account_close(
        str(ledger_workspace), "Assets:Cash", "2026-07-17"
    )
    assert isinstance(nonzero, InvariantViolation)
    assert nonzero.invariant == "ACCOUNT_NONZERO_INVENTORY"
    assert "CNY" in str(nonzero.provided)

    month_file = ledger_workspace / f"data/agent_inc/{date.today():%Y-%m}.beancount"
    month_file.write_text(
        month_file.read_text()
        + '\n2026-07-18 * "Future salary posting"\n'
        + "  Income:Salary  1 CNY\n"
        + "  Assets:Cash   -1 CNY\n"
    )
    future = LedgerService().prepare_account_close(
        str(ledger_workspace), "Income:Salary", "2026-07-17"
    )
    assert isinstance(future, InvariantViolation)
    assert future.invariant == "ACCOUNT_HAS_FUTURE_POSTINGS"


def test_account_close_rejects_invalid_lifecycle_requests(ledger_workspace: Path) -> None:
    missing = AccountClosePreparationHandler().build(
        str(ledger_workspace), account_name="Assets:Missing", close_date="2026-07-17"
    )
    before_open = AccountClosePreparationHandler().build(
        str(ledger_workspace), account_name="Income:Salary", close_date="2019-12-31"
    )
    closed_file = ledger_workspace / f"data/agent_inc/{date.today():%Y-%m}.beancount"
    closed_file.write_text(closed_file.read_text() + "\n2026-07-16 close Income:Salary\n")
    already_closed = AccountClosePreparationHandler().build(
        str(ledger_workspace), account_name="Income:Salary", close_date="2026-07-17"
    )

    assert isinstance(missing, InvariantViolation)
    assert missing.invariant == "ACCOUNT_NOT_FOUND"
    assert isinstance(before_open, InvariantViolation)
    assert before_open.invariant == "ACCOUNT_CLOSE_BEFORE_OPEN"
    assert isinstance(already_closed, InvariantViolation)
    assert already_closed.invariant == "ACCOUNT_ALREADY_CLOSED"


def test_account_close_apply_requires_proof_and_publishes_exact_plan(
    ledger_workspace: Path,
    monkeypatch,
) -> None:
    pending = LedgerService().prepare_account_close(
        str(ledger_workspace), "Income:Salary", "2026-07-17", "close salary"
    )
    assert isinstance(pending, PendingAction)
    publisher = Mock()
    publisher.commit_and_push.return_value = {
        "ok": True,
        "error": None,
        "push": "PUSHED: ok",
        "commit_sha": "close-sha",
    }
    rejected = LedgerService().apply_pending_action(
        str(ledger_workspace), pending.__dict__.copy(), "repo", publisher
    )
    assert rejected.status == "INTEGRITY_FAILED"
    publisher.commit_and_push.assert_not_called()

    proof = {
        "approved_by": "user_123",
        "approved_at": "2026-07-18T00:00:00Z",
        "approval_id": "approval-close",
        "pending_action_id": pending.pending_action_id,
        "payload_digest": digest_payload(pending.__dict__.copy()),
        "integrity_digest": pending.digest,
        "host": "test-host",
    }
    monkeypatch.setattr("agent_core.services.beancount.Beancount.bean_format", lambda *_args: None)
    applied = LedgerService().apply_pending_action(
        str(ledger_workspace),
        pending.__dict__.copy(),
        "repo",
        publisher,
        approval_proof=proof,
    )
    assert applied.status == "APPLIED"
    assert "2026-07-17 close Income:Salary" in (
        ledger_workspace / f"data/agent_inc/{date.today():%Y-%m}.beancount"
    ).read_text()
