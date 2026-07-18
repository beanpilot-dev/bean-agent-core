"""Deterministic account lifecycle and close-cutoff inspection helpers."""

import os
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .beancount import Beancount
from .reconciliation import format_decimal
from .types import LedgerConfig


@dataclass(frozen=True)
class AccountPostingFact:
    """Safe source facts for one posting to an exact account."""

    date: str
    source_path: str | None
    source_line: int | None


@dataclass(frozen=True)
class AccountCloseState:
    """All deterministic read facts needed to prepare one account close."""

    account_name: str
    close_date: str
    status: str | None
    open_date: str | None
    existing_close_date: str | None
    last_posting_date: str | None
    last_posting_date_at_or_before_close: str | None
    future_postings: tuple[AccountPostingFact, ...]
    inventory: tuple[str, ...]
    inventory_commodities: tuple[str, ...]


def _source_fact(workspace: str, posting: object) -> tuple[str | None, int | None]:
    metadata = getattr(posting, "meta", None)
    filename = metadata.get("filename") if isinstance(metadata, dict) else None
    source_path: str | None = None
    if isinstance(filename, str) and filename:
        try:
            relative = os.path.relpath(filename, workspace).replace(os.sep, "/")
            source_path = relative if not relative.startswith("../") else filename
        except (OSError, ValueError):
            source_path = filename
    line = metadata.get("lineno") if isinstance(metadata, dict) else None
    return source_path, line if isinstance(line, int) else None


def inspect_account_close(
    workspace: str,
    account_name: str,
    close_date: date,
    ledger_config: LedgerConfig | None = None,
) -> AccountCloseState | None:
    """Inspect exact-account lifecycle, postings, and inventory at a cutoff.

    The calculation intentionally considers postings to the exact account only.
    Descendant accounts have independent Beancount lifecycles and are not
    silently included in an exact-account close decision.
    """
    parsed = Beancount.parsed_ledger(workspace, ledger_config)
    account = next(
        (candidate for candidate in parsed.account_index if candidate.account_name == account_name),
        None,
    )
    if account is None:
        return None

    inventory_totals: dict[str, Decimal] = {}
    posting_facts: list[AccountPostingFact] = []
    future_postings: list[AccountPostingFact] = []
    for entry in parsed.entries:
        if entry.__class__.__name__ != "Transaction":
            continue
        entry_date = getattr(entry, "date", None)
        if not isinstance(entry_date, date):
            continue
        for posting in getattr(entry, "postings", ()) or ():
            if getattr(posting, "account", None) != account_name:
                continue
            source_path, source_line = _source_fact(workspace, posting)
            fact = AccountPostingFact(entry_date.isoformat(), source_path, source_line)
            posting_facts.append(fact)
            if entry_date > close_date:
                future_postings.append(fact)
                continue
            units = getattr(posting, "units", None)
            currency = getattr(units, "currency", None)
            number = getattr(units, "number", None)
            if not isinstance(currency, str):
                continue
            try:
                amount = Decimal(str(number))
            except (InvalidOperation, TypeError, ValueError):
                continue
            inventory_totals[currency] = inventory_totals.get(currency, Decimal("0")) + amount

    all_dates = [date.fromisoformat(fact.date) for fact in posting_facts]
    cutoff_dates = [
        date.fromisoformat(fact.date)
        for fact in posting_facts
        if date.fromisoformat(fact.date) <= close_date
    ]
    inventory_positions = tuple(
        f"{format_decimal(amount)} {currency}"
        for currency, amount in sorted(inventory_totals.items())
        if amount != 0
    )
    commodities = tuple(sorted(inventory_totals))
    return AccountCloseState(
        account_name=account_name,
        close_date=close_date.isoformat(),
        status=account.status,
        open_date=account.open_date.isoformat() if account.open_date else None,
        existing_close_date=account.close_date.isoformat() if account.close_date else None,
        last_posting_date=max(all_dates).isoformat() if all_dates else None,
        last_posting_date_at_or_before_close=(
            max(cutoff_dates).isoformat() if cutoff_dates else None
        ),
        future_postings=tuple(future_postings),
        inventory=inventory_positions,
        inventory_commodities=commodities,
    )


def account_close_state_payload(state: AccountCloseState) -> dict[str, Any]:
    """Return stable, JSON-safe facts for previews and semantic fact hashing."""
    payload = asdict(state)
    payload["future_postings"] = [asdict(posting) for posting in state.future_postings]
    return payload
