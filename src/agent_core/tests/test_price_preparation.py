from datetime import date
from pathlib import Path
from unittest.mock import Mock

from agent_core.services.ledger import LedgerService
from agent_core.services.types import InvariantViolation, PendingAction


def _inputs() -> dict[str, str]:
    return {
        "price_date": "2026-07-17",
        "base_commodity": "AAPL",
        "price": "213.45",
        "quote_commodity": "USD",
        "source": "user supplied brokerage statement",
        "effective_at": "2026-07-17T09:30:00+08:00",
        "commit_message": "record AAPL price",
    }


def test_price_preparation_is_explicit_and_does_not_fetch(
    ledger_workspace: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "agent_core.services.prices.PriceService.fetch_market_price",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )
    result = LedgerService().prepare_price(str(ledger_workspace), **_inputs())

    assert isinstance(result, PendingAction)
    assert result.action_type == "record_price"
    assert result.display["kind"] == "price_preview"
    assert "2026-07-17 price AAPL 213.45 USD" in result.display["directive"]
    assert result.display["source"] == "user supplied brokerage statement"
    assert result.execution_spec["price"] == "213.45"
    assert result.execution_spec["source"] == "user supplied brokerage statement"


def test_price_preparation_rejects_exact_and_conflicting_duplicates(
    ledger_workspace: Path,
) -> None:
    path = ledger_workspace / f"data/agent_inc/{date.today():%Y-%m}.beancount"
    path.write_text(path.read_text() + "\n2026-07-17 price AAPL 213.45 USD\n")
    exact = LedgerService().prepare_price(str(ledger_workspace), **_inputs())
    assert isinstance(exact, InvariantViolation)
    assert exact.invariant == "PRICE_ALREADY_RECORDED"

    path.write_text(path.read_text().replace("213.45", "214.00"))
    conflict = LedgerService().prepare_price(str(ledger_workspace), **_inputs())
    assert isinstance(conflict, InvariantViolation)
    assert conflict.invariant == "PRICE_CONFLICT"


def test_price_apply_fails_closed_when_sidecar_changes_after_prepare(
    ledger_workspace: Path,
    monkeypatch,
) -> None:
    pending = LedgerService().prepare_price(str(ledger_workspace), **_inputs())
    assert isinstance(pending, PendingAction)
    path = ledger_workspace / f"data/agent_inc/{date.today():%Y-%m}.beancount"
    path.write_text(path.read_text() + "\n2026-07-18 price MSFT 400 USD\n")
    publisher = Mock()

    result = LedgerService().apply_pending_action(
        str(ledger_workspace), pending.__dict__.copy(), "repo", publisher
    )

    assert result.status == "INVARIANT_VIOLATION"
    assert result.invariant == "MUTATION_PRECONDITION_FAILED"
    publisher.commit_and_push.assert_not_called()
