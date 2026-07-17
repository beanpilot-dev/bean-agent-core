"""Characterize semantic inputs that a sealed mutation plan must protect.

These tests describe the v2 stale-read contract: operation write targets and
handler-declared semantic facts are sealed, while unrelated repository and
ledger inputs remain outside the approval's read set.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_core.services.beancount import Beancount
from agent_core.services.ledger import LedgerService
from agent_core.services.mutations.facts import (
    SemanticFact,
    capture_account_state_fact,
    capture_balance_fact,
    capture_checkpoint_fact,
    semantic_facts_hold,
)
from agent_core.services.queries import LedgerQueryService
from agent_core.services.types import (
    ApplyReceipt,
    InvariantViolation,
    LedgerConfig,
    PendingAction,
)

DINNER = (
    '2026-06-15 * "Dinner"\n'
    "  Expenses:Food:Dining  100 CNY\n"
    "  Assets:Cash          -100 CNY"
)


def _publisher() -> Mock:
    publisher = Mock()
    publisher.commit_and_push.return_value = {"ok": True, "error": None, "push": "PUSHED"}
    return publisher


def _transaction_detail(workspace: Path) -> dict[str, object]:
    found = LedgerQueryService.find_transactions(
        str(workspace), narration_contains="Lunch"
    )
    return LedgerQueryService.get_transaction(
        str(workspace), found.rows[0]["transaction_ref"]
    ).transaction or {}


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


def _move_lunch_to_historical_file(workspace: Path) -> tuple[Path, Path]:
    """Put the fixture transaction in a replace-only non-current target."""
    sidecar = workspace / "data" / "agent_inc"
    current = sidecar / f"{date.today():%Y-%m}.beancount"
    history = sidecar / "history.beancount"
    history.write_text(current.read_text())
    current.write_text("")
    main = sidecar / "main.beancount"
    main.write_text(main.read_text() + 'include "history.beancount"\n')
    return history, current


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


def test_commit_allows_unrelated_checkpoint_added_in_included_source(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handler's narrow facts do not seal unrelated included-ledger content."""
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    included = _add_primary_include(ledger_workspace, "checkpoints.beancount")
    pending = LedgerService().prepare_commit(str(ledger_workspace), DINNER, "record dinner")
    assert isinstance(pending, PendingAction)

    included.write_text("2026-06-01 balance Assets:Bank:Checking  5000 CNY\n")
    publisher = _publisher()
    result = LedgerService().apply_pending_action(
        str(ledger_workspace), pending.__dict__.copy(), "repo", publisher
    )

    assert isinstance(result, ApplyReceipt)
    publisher.commit_and_push.assert_called_once()


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


def test_replace_plan_allows_unrelated_current_month_ledger_change(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replace replay seals and publishes only its explicit historical target."""
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    history, current = _move_lunch_to_historical_file(ledger_workspace)
    replacement = (
        '2026-05-12 * "Lunch"\n'
        "  Expenses:Food:Dining  95 CNY\n"
        "  Assets:Cash          -95 CNY"
    )
    detail = _transaction_detail(ledger_workspace)
    pending = LedgerService().prepare_transaction_update(
        str(ledger_workspace),
        detail["transaction_ref"],
        detail["revision_fingerprint"],
        replacement,
        "update lunch",
    )
    assert isinstance(pending, PendingAction)

    current.write_text(
        '2026-06-20 * "Unrelated bank adjustment"\n'
        "  Assets:Bank:Checking       10 CNY\n"
        "  Equity:Opening-Balances   -10 CNY\n"
    )
    publisher = _publisher()
    result = LedgerService().apply_pending_action(
        str(ledger_workspace), pending.__dict__.copy(), "repo", publisher
    )

    assert isinstance(result, ApplyReceipt)
    assert "95 CNY" in history.read_text()
    assert "Unrelated bank adjustment" in current.read_text()
    assert publisher.commit_and_push.call_args.args[4] == (
        "data/agent_inc/history.beancount",
    )


def test_replace_plan_rejects_change_to_its_explicit_target(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    history, _current = _move_lunch_to_historical_file(ledger_workspace)
    replacement = (
        '2026-05-12 * "Lunch"\n'
        "  Expenses:Food:Dining  95 CNY\n"
        "  Assets:Cash          -95 CNY"
    )
    detail = _transaction_detail(ledger_workspace)
    pending = LedgerService().prepare_transaction_update(
        str(ledger_workspace),
        detail["transaction_ref"],
        detail["revision_fingerprint"],
        replacement,
        "update lunch",
    )
    assert isinstance(pending, PendingAction)

    history.write_text(history.read_text() + "; concurrent target edit\n")
    publisher = _publisher()
    result = LedgerService().apply_pending_action(
        str(ledger_workspace), pending.__dict__.copy(), "repo", publisher
    )

    _assert_stale(result, publisher)


def test_account_fact_hashes_lifecycle_and_accepts_historic_presence_digest(
    ledger_workspace: Path,
) -> None:
    account = "Expenses:Food:Dining"
    lifecycle_fact = capture_account_state_fact(str(ledger_workspace), account)
    legacy_fact = SemanticFact(
        "account_state", account, hashlib.sha256(b"present").hexdigest()
    )
    legacy_absent_fact = SemanticFact(
        "account_state",
        "Assets:Bank:Not-Open",
        hashlib.sha256(b"absent").hexdigest(),
    )

    assert semantic_facts_hold(str(ledger_workspace), (lifecycle_fact,))
    assert semantic_facts_hold(str(ledger_workspace), (legacy_fact,))
    assert semantic_facts_hold(str(ledger_workspace), (legacy_absent_fact,))

    included = _add_primary_include(ledger_workspace, "lifecycle-change.beancount")
    included.write_text("2026-06-14 close Expenses:Food:Dining\n")

    assert not semantic_facts_hold(str(ledger_workspace), (lifecycle_fact,))
    assert semantic_facts_hold(str(ledger_workspace), (legacy_fact,))

    included.write_text(
        included.read_text() + "2020-01-01 open Assets:Bank:Not-Open CNY\n"
    )
    assert not semantic_facts_hold(str(ledger_workspace), (legacy_absent_fact,))


def test_balance_and_checkpoint_facts_reject_relevant_ledger_changes(
    ledger_workspace: Path,
) -> None:
    balance = capture_balance_fact(
        str(ledger_workspace), "Assets:Bank:Checking", "2026-06-01"
    )
    checkpoint = capture_checkpoint_fact(
        str(ledger_workspace),
        "Assets:Bank:Checking",
        "2026-06-01",
        "CNY",
    )
    included = _add_primary_include(ledger_workspace, "reconciliation-inputs.beancount")

    included.write_text(
        '2026-05-30 * "Late bank import"\n'
        "  Assets:Bank:Checking          10 CNY\n"
        "  Equity:Opening-Balances       -10 CNY\n"
    )
    assert not semantic_facts_hold(str(ledger_workspace), (balance,))
    assert semantic_facts_hold(str(ledger_workspace), (checkpoint,))

    included.write_text(
        included.read_text()
        + "\n2026-06-01 balance Assets:Bank:Checking  5010 CNY\n"
    )
    assert not semantic_facts_hold(str(ledger_workspace), (checkpoint,))


def test_narrow_reconciliation_facts_allow_unrelated_included_file_changes(
    ledger_workspace: Path,
) -> None:
    facts = (
        capture_account_state_fact(str(ledger_workspace), "Assets:Bank:Checking"),
        capture_account_state_fact(str(ledger_workspace), "Equity:Opening-Balances"),
        capture_balance_fact(
            str(ledger_workspace), "Assets:Bank:Checking", "2026-06-01"
        ),
        capture_checkpoint_fact(
            str(ledger_workspace),
            "Assets:Bank:Checking",
            "2026-06-01",
            "CNY",
        ),
    )
    included = _add_primary_include(ledger_workspace, "unrelated-posting.beancount")

    included.write_text(
        '2026-06-10 * "Unrelated purchase"\n'
        "  Expenses:Food:Dining   5 CNY\n"
        "  Assets:Cash           -5 CNY\n"
    )

    assert semantic_facts_hold(str(ledger_workspace), facts)


def test_narrow_facts_recompute_with_custom_ledger_config(tmp_path: Path) -> None:
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
        "2020-01-01 open Equity:Opening-Balances CNY\n"
        '2020-01-01 * "Opening"\n'
        "  Assets:Cash               100 CNY\n"
        "  Equity:Opening-Balances  -100 CNY\n"
    )
    facts = (
        capture_account_state_fact(str(tmp_path), "Assets:Cash", config),
        capture_balance_fact(str(tmp_path), "Assets:Cash", "2026-01-01", config),
        capture_checkpoint_fact(
            str(tmp_path), "Assets:Cash", "2026-01-01", "CNY", config
        ),
    )

    assert semantic_facts_hold(str(tmp_path), facts, config)

    (sidecar / "main.beancount").write_text(
        (sidecar / "main.beancount").read_text()
        + '2025-12-31 * "Relevant import"\n'
        "  Assets:Cash                 1 CNY\n"
        "  Equity:Opening-Balances    -1 CNY\n"
    )
    assert not semantic_facts_hold(str(tmp_path), facts, config)


@pytest.mark.parametrize(
    "fact",
    [
        SemanticFact("unknown", "anything", None),
        SemanticFact("account_state", "not-an-account", None),
        SemanticFact("balance_state", '{"account":"Assets:Cash"}', "digest"),
        SemanticFact(
            "checkpoint_state",
            '{"account":"Assets:Cash","currency":"CNY","date":"not-a-date"}',
            "digest",
        ),
        SemanticFact("checkpoint_state", "not-json", "digest"),
    ],
)
def test_unknown_and_malformed_semantic_facts_fail_closed(
    ledger_workspace: Path, fact: SemanticFact
) -> None:
    assert not semantic_facts_hold(str(ledger_workspace), (fact,))
