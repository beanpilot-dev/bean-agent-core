"""Bounded, Beancount-native facts derived from a parsed ledger."""

from __future__ import annotations

import calendar
import time
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

NATIVE_ACCOUNT_TYPES = ("Assets", "Liabilities", "Equity", "Income", "Expenses")
MAX_CONTEXT_ACCOUNTS = 120
MAX_BALANCE_ACCOUNTS = 50
MAX_FLOW_MONTHS = 6
MAX_RECENT_TRANSACTIONS = 8
MAX_RECENT_POSTINGS_PER_TRANSACTION = 12
MAX_RECENT_LABELS = 12
MAX_COMMODITIES = 64
MAX_FLOW_COMMODITY_ROWS = 32
MAX_RECENT_LEDGER_TEXT_CHARS = 4_000
MAX_LEDGER_CONTEXT_CHARS = 24_000


def _account_type(account: object) -> str | None:
    if not isinstance(account, str):
        return None
    root = account.split(":", 1)[0]
    return root if root in NATIVE_ACCOUNT_TYPES else None


def _decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None


def _number(value: object) -> str:
    decimal = _decimal(value) or Decimal("0")
    if decimal == 0:
        return "0"
    return format(decimal, "f")


def _posting_amount(posting: object) -> tuple[str, Decimal] | None:
    units = getattr(posting, "units", None)
    commodity = getattr(units, "currency", None)
    number = _decimal(getattr(units, "number", None))
    if not isinstance(commodity, str) or number is None:
        return None
    return commodity, number


def _transactions(entries: Iterable[object]) -> list[object]:
    return [entry for entry in entries if entry.__class__.__name__ == "Transaction"]


def _entry_accounts(entries: Iterable[object]) -> set[str]:
    accounts: set[str] = set()
    for entry in entries:
        account = getattr(entry, "account", None)
        if _account_type(account):
            accounts.add(account)
        for posting in getattr(entry, "postings", ()) or ():
            posting_account = getattr(posting, "account", None)
            if _account_type(posting_account):
                accounts.add(posting_account)
    return accounts


def all_accounts(entries: Iterable[object]) -> list[str]:
    """Return every exact native account name for compatibility consumers."""
    return sorted(_entry_accounts(entries))


def _group_accounts(accounts: set[str]) -> tuple[dict[str, list[str]], bool, int]:
    grouped = {
        root: sorted(account for account in accounts if _account_type(account) == root)
        for root in NATIVE_ACCOUNT_TYPES
    }
    total = sum(len(values) for values in grouped.values())
    if total <= MAX_CONTEXT_ACCOUNTS:
        return grouped, False, 0

    selected = {root: [] for root in NATIVE_ACCOUNT_TYPES}
    remaining = {root: list(values) for root, values in grouped.items()}
    while sum(len(values) for values in selected.values()) < MAX_CONTEXT_ACCOUNTS:
        progressed = False
        for root in NATIVE_ACCOUNT_TYPES:
            if (
                remaining[root]
                and sum(len(values) for values in selected.values()) < MAX_CONTEXT_ACCOUNTS
            ):
                selected[root].append(remaining[root].pop(0))
                progressed = True
        if not progressed:
            break
    return selected, True, total - MAX_CONTEXT_ACCOUNTS


def _active_accounts(entries: Iterable[object], as_of: date, known: set[str]) -> set[str]:
    opened: set[str] = set()
    closed: set[str] = set()
    for entry in entries:
        entry_date = getattr(entry, "date", None)
        account = getattr(entry, "account", None)
        if not isinstance(entry_date, date) or not isinstance(account, str):
            continue
        if entry_date > as_of or not _account_type(account):
            continue
        if entry.__class__.__name__ == "Open":
            opened.add(account)
        elif entry.__class__.__name__ == "Close":
            closed.add(account)
    return (opened or known) - closed


def _month_offset(year: int, month: int, offset: int) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + offset
    return absolute // 12, absolute % 12 + 1


def _month_name(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _monthly_amounts(
    transactions: Iterable[object], as_of: date
) -> dict[str, dict[str, dict[str, Decimal]]]:
    monthly: dict[str, dict[str, dict[str, Decimal]]] = defaultdict(
        lambda: {"income": defaultdict(Decimal), "expenses": defaultdict(Decimal)}
    )
    for transaction in transactions:
        transaction_date = getattr(transaction, "date", None)
        if not isinstance(transaction_date, date) or transaction_date > as_of:
            continue
        month = _month_name(transaction_date.year, transaction_date.month)
        for posting in getattr(transaction, "postings", ()) or ():
            root = _account_type(getattr(posting, "account", None))
            amount = _posting_amount(posting)
            if root not in {"Income", "Expenses"} or amount is None:
                continue
            commodity, number = amount
            key = "income" if root == "Income" else "expenses"
            # Beancount income postings are credits (negative); expose the
            # conventional positive income magnitude while preserving signs for
            # expense refunds and corrections.
            monthly[month][key][commodity] += -number if root == "Income" else number
    return monthly


def _amount_rows(values: dict[str, Decimal]) -> tuple[list[dict[str, str]], bool, int]:
    rows = [
        {"commodity": commodity, "amount": _number(values[commodity])}
        for commodity in sorted(values)
        if values[commodity] != 0
    ]
    return rows[:MAX_FLOW_COMMODITY_ROWS], len(rows) > MAX_FLOW_COMMODITY_ROWS, max(
        0, len(rows) - MAX_FLOW_COMMODITY_ROWS
    )


def _flow_month(name: str, values: dict[str, dict[str, Decimal]]) -> dict[str, Any]:
    income, income_truncated, income_omitted = _amount_rows(values["income"])
    expenses, expenses_truncated, expenses_omitted = _amount_rows(values["expenses"])
    result: dict[str, Any] = {"month": name, "income": income, "expenses": expenses}
    if income_truncated:
        result["income_truncated"] = True
        result["income_omitted"] = income_omitted
    if expenses_truncated:
        result["expenses_truncated"] = True
        result["expenses_omitted"] = expenses_omitted
    return result


def _flow_summary(transactions: list[object], as_of: date) -> dict[str, Any]:
    monthly = _monthly_amounts(transactions, as_of)
    complete_months: list[dict[str, Any]] = []
    for offset in range(-MAX_FLOW_MONTHS, 0):
        year, month = _month_offset(as_of.year, as_of.month, offset)
        name = _month_name(year, month)
        values = monthly.get(name, {"income": {}, "expenses": {}})
        complete_months.append(_flow_month(name, values))
    current_name = _month_name(as_of.year, as_of.month)
    current = monthly.get(current_name, {"income": {}, "expenses": {}})
    current_partial = _flow_month(current_name, current)
    current_partial["through"] = as_of.isoformat()
    current_partial = {
        "month": current_partial.pop("month"),
        "through": current_partial.pop("through"),
        **current_partial,
    }
    return {
        "basis": "beancount_income_and_expense_postings",
        "complete_months": complete_months,
        "current_partial_month": current_partial,
    }


def _ledger_metadata(
    entries: list[object],
    transactions: list[object],
    known_accounts: set[str],
    as_of: date,
    bean_check_passed: bool,
) -> dict[str, Any]:
    transaction_dates = sorted(
        getattr(transaction, "date")
        for transaction in transactions
        if isinstance(getattr(transaction, "date", None), date)
    )
    active = _active_accounts(entries, as_of, known_accounts)
    account_counts = {
        root: sum(1 for account in active if _account_type(account) == root)
        for root in NATIVE_ACCOUNT_TYPES
    }
    commodities: set[str] = set()
    for transaction in transactions:
        for posting in getattr(transaction, "postings", ()) or ():
            amount = _posting_amount(posting)
            if amount is not None:
                commodities.add(amount[0])
    for entry in entries:
        amount = getattr(entry, "amount", None)
        commodity = getattr(amount, "currency", None)
        if isinstance(commodity, str) and entry.__class__.__name__ == "Balance":
            commodities.add(commodity)

    commodity_list = sorted(commodities)
    metadata: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "date_range": {},
        "current_month_is_partial": as_of.day
        < calendar.monthrange(as_of.year, as_of.month)[1],
        "commodities": commodity_list[:MAX_COMMODITIES],
        "account_counts": account_counts,
        "bean_check_passed": bean_check_passed,
    }
    if transaction_dates:
        metadata["date_range"] = {
            "from": transaction_dates[0].isoformat(),
            "to": transaction_dates[-1].isoformat(),
        }
    if len(commodity_list) > MAX_COMMODITIES:
        metadata["commodities_truncated"] = True
        metadata["commodities_omitted"] = len(commodity_list) - MAX_COMMODITIES
    return metadata
def _balance_snapshot(transactions: list[object], as_of: date) -> dict[str, Any]:
    positions: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for transaction in transactions:
        transaction_date = getattr(transaction, "date", None)
        if not isinstance(transaction_date, date) or transaction_date > as_of:
            continue
        for posting in getattr(transaction, "postings", ()) or ():
            account = getattr(posting, "account", None)
            root = _account_type(account)
            amount = _posting_amount(posting)
            if (
                not isinstance(account, str)
                or root not in {"Assets", "Liabilities", "Equity"}
                or amount is None
            ):
                continue
            commodity, number = amount
            positions[account][commodity] += number

    nonzero_accounts = [
        account for account in positions if any(value != 0 for value in positions[account].values())
    ]
    ordered = sorted(
        nonzero_accounts,
        key=lambda account: (
            NATIVE_ACCOUNT_TYPES.index(_account_type(account) or "Assets"), account
        ),
    )
    selected = ordered[:MAX_BALANCE_ACCOUNTS]
    return {
        "as_of": as_of.isoformat(),
        "accounts": [
            {
                "account": account,
                "type": _account_type(account),
                "positions": [
                    {"number": _number(number), "commodity": commodity}
                    for commodity, number in sorted(positions[account].items())
                    if number != 0
                ],
            }
            for account in selected
        ],
        "truncated": len(selected) < len(ordered),
        "omitted_accounts": max(0, len(ordered) - len(selected)),
    }


def _recent_transaction(transaction: object) -> dict[str, Any]:
    result: dict[str, Any] = {"date": getattr(transaction, "date").isoformat()}
    for key in ("flag", "payee", "narration"):
        value = getattr(transaction, key, None)
        if value is not None:
            result[key] = value
    tags = sorted(getattr(transaction, "tags", ()) or ())
    links = sorted(getattr(transaction, "links", ()) or ())
    if tags:
        result["tags"] = tags[:MAX_RECENT_LABELS]
        result["tags_truncated"] = len(tags) > MAX_RECENT_LABELS
    if links:
        result["links"] = links[:MAX_RECENT_LABELS]
        result["links_truncated"] = len(links) > MAX_RECENT_LABELS
    postings: list[dict[str, Any]] = []
    all_postings = list(getattr(transaction, "postings", ()) or ())
    for posting in all_postings[:MAX_RECENT_POSTINGS_PER_TRANSACTION]:
        account = getattr(posting, "account", None)
        amount = _posting_amount(posting)
        if not isinstance(account, str):
            continue
        item: dict[str, Any] = {"account": account}
        if amount is not None:
            commodity, number = amount
            item.update({"number": _number(number), "commodity": commodity})
        postings.append(item)
    result["postings"] = postings
    result["postings_truncated"] = len(all_postings) > MAX_RECENT_POSTINGS_PER_TRANSACTION
    result["postings_omitted"] = max(
        0, len(all_postings) - MAX_RECENT_POSTINGS_PER_TRANSACTION
    )
    return result


def _recent_activity(transactions: list[object], as_of: date) -> dict[str, Any]:
    eligible = [
        transaction
        for transaction in transactions
        if isinstance(getattr(transaction, "date", None), date)
        and getattr(transaction, "date") <= as_of
    ]
    eligible.sort(key=lambda transaction: getattr(transaction, "date"))
    selected = eligible[-MAX_RECENT_TRANSACTIONS:]
    return {
        "transactions": [_recent_transaction(transaction) for transaction in selected],
        "truncated": len(selected) < len(eligible),
        "omitted_transactions": max(0, len(eligible) - len(selected)),
    }


def _recent_ledger_text(
    raw_text: str, max_chars: int = MAX_RECENT_LEDGER_TEXT_CHARS
) -> dict[str, Any]:
    truncated = len(raw_text) > max_chars
    return {
        "text": raw_text[-max_chars:] if truncated else raw_text,
        "truncated": truncated,
    }


def _context_size(context: dict[str, Any]) -> int:
    import json

    return len(json.dumps(context, ensure_ascii=False, separators=(",", ":")))


def _enforce_context_budget(context: dict[str, Any]) -> dict[str, Any]:
    if _context_size(context) <= MAX_LEDGER_CONTEXT_CHARS:
        context["context_truncated"] = False
        if _context_size(context) <= MAX_LEDGER_CONTEXT_CHARS:
            return context

    context["context_truncated"] = True
    raw = context.get("recent_ledger_text")
    if isinstance(raw, dict):
        raw["text"] = str(raw.get("text", ""))[-1_000:]
        raw["truncated"] = True
    activity = context.get("recent_activity")
    if isinstance(activity, dict):
        transactions = activity.get("transactions")
        if isinstance(transactions, list) and len(transactions) > 4:
            activity["transactions"] = transactions[-4:]
            activity["truncated"] = True
            activity["omitted_transactions"] = max(
                activity.get("omitted_transactions", 0), len(transactions) - 4
            )
    balance = context.get("balance_snapshot")
    if isinstance(balance, dict) and isinstance(balance.get("accounts"), list):
        accounts = balance["accounts"]
        if len(accounts) > 20:
            balance["accounts"] = accounts[:20]
            balance["truncated"] = True
            balance["omitted_accounts"] = max(
                balance.get("omitted_accounts", 0), len(accounts) - 20
            )
    accounts = context.get("accounts")
    if isinstance(accounts, dict):
        for root in NATIVE_ACCOUNT_TYPES:
            values = accounts.get(root)
            if isinstance(values, list):
                accounts[root] = values[:24]
    while _context_size(context) > MAX_LEDGER_CONTEXT_CHARS:
        changed = False
        if isinstance(raw, dict) and len(str(raw.get("text", ""))) > 200:
            raw["text"] = str(raw.get("text", ""))[-200:]
            raw["truncated"] = True
            changed = True
        elif isinstance(activity, dict) and isinstance(activity.get("transactions"), list):
            transactions = activity["transactions"]
            if transactions:
                activity["transactions"] = transactions[1:]
                activity["truncated"] = True
                activity["omitted_transactions"] = activity.get("omitted_transactions", 0) + 1
                changed = True
        elif isinstance(balance, dict) and isinstance(balance.get("accounts"), list):
            balance_accounts = balance["accounts"]
            if balance_accounts:
                balance["accounts"] = balance_accounts[:-1]
                balance["truncated"] = True
                balance["omitted_accounts"] = balance.get("omitted_accounts", 0) + 1
                changed = True
        elif isinstance(accounts, dict):
            for root in reversed(NATIVE_ACCOUNT_TYPES):
                values = accounts.get(root)
                if isinstance(values, list) and values:
                    values.pop()
                    context["accounts_truncated"] = True
                    context["accounts_omitted"] = context.get("accounts_omitted", 0) + 1
                    changed = True
                    break
        if not changed:
            break

    if _context_size(context) > MAX_LEDGER_CONTEXT_CHARS:
        context["recent_ledger_text"] = {"text": "", "truncated": True}
        context["recent_activity"] = {
            "transactions": [],
            "truncated": True,
            "omitted_transactions": context.get("recent_activity", {}).get(
                "omitted_transactions", 0
            ),
        }
        context["balance_snapshot"] = {
            "accounts": [],
            "truncated": True,
            "omitted_accounts": context.get("balance_snapshot", {}).get(
                "omitted_accounts", 0
            ),
        }
        context["accounts"] = {root: [] for root in NATIVE_ACCOUNT_TYPES}
        context["accounts_truncated"] = True
    return context


def build_ledger_context(
    entries: list[object],
    *,
    as_of: date,
    target: str,
    raw_text: str,
    bean_check_passed: bool,
    timings_ms: dict[str, float] | None = None,
) -> dict[str, Any]:
    def timed(name: str, callback):
        started = time.perf_counter()
        value = callback()
        if timings_ms is not None:
            timings_ms[name] = round((time.perf_counter() - started) * 1000, 2)
        return value

    transactions = _transactions(entries)
    known_accounts = _entry_accounts(entries)
    grouped, accounts_truncated, accounts_omitted = timed(
        "account_extraction", lambda: _group_accounts(known_accounts)
    )
    ledger_meta: dict[str, Any] = timed(
        "ledger_metadata",
        lambda: _ledger_metadata(
            entries, transactions, known_accounts, as_of, bean_check_passed
        ),
    )

    balance_snapshot = timed("balance_snapshot", lambda: _balance_snapshot(transactions, as_of))
    flow_summary = timed("flow_summary", lambda: _flow_summary(transactions, as_of))
    recent_activity = timed("recent_context", lambda: _recent_activity(transactions, as_of))
    recent_ledger_text = timed("recent_ledger_text", lambda: _recent_ledger_text(raw_text))
    context = {
        "status": "CLEAN" if bean_check_passed else "ERROR",
        "target": target,
        "accounts": grouped,
        "accounts_truncated": accounts_truncated,
        "accounts_omitted": accounts_omitted,
        "ledger_meta": ledger_meta,
        "balance_snapshot": balance_snapshot,
        "flow_summary": flow_summary,
        "recent_activity": recent_activity,
        "recent_ledger_text": recent_ledger_text,
        "errors": None if bean_check_passed else "bean-check failed",
    }
    return _enforce_context_budget(context)
