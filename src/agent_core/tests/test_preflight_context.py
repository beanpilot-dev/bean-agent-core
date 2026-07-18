"""Tests for compact Beancount-native preflight context."""

import json
from datetime import date
from textwrap import dedent

from beancount import loader

from agent_core.services.preflight_context import (
    MAX_CONTEXT_ACCOUNTS,
    MAX_LEDGER_CONTEXT_CHARS,
    MAX_RECENT_TRANSACTIONS,
    build_ledger_context,
)


def _context(text: str, *, as_of: date = date(2026, 7, 16)) -> dict:
    entries, errors, _options = loader.load_string(dedent(text))
    assert not errors
    return build_ledger_context(
        list(entries),
        as_of=as_of,
        target="data/agent_inc/2026-07.beancount",
        raw_text=text,
        bean_check_passed=True,
    )


def test_context_reports_native_metadata_balances_and_flows_without_reclassification():
    context = _context(
        """
        option "operating_currency" "CNY"
        option "operating_currency" "USD"
        2025-01-01 commodity USD
        2025-12-31 open Assets:Bank CNY
        2025-12-31 open Assets:Savings CNY
        2025-12-31 open Assets:Broker USD
        2025-12-31 open Liabilities:Card CNY
        2025-12-31 open Equity:Opening CNY
        2025-12-31 open Equity:USD-Opening USD
        2025-12-31 open Income:Salary CNY
        2025-12-31 open Expenses:Food CNY
        2026-05-10 * "Salary"
          Assets:Bank 1000 CNY
          Income:Salary -1000 CNY
        2026-06-12 * "Dinner"
          Expenses:Food 80 CNY
          Assets:Bank -80 CNY
        2026-06-13 * "Transfer"
          Assets:Bank -200 CNY
          Assets:Savings 200 CNY
        2026-06-14 * "Card payment"
          Assets:Bank -500 CNY
          Liabilities:Card 500 CNY
        2026-07-15 * "Foreign holding"
          Assets:Broker 10 USD
          Equity:USD-Opening -10 USD
        """
    )

    assert context["ledger_meta"] == {
        "as_of": "2026-07-16",
        "date_range": {"from": "2026-05-10", "to": "2026-07-15"},
        "current_month_is_partial": True,
        "commodities": ["CNY", "USD"],
        "account_counts": {
            "Assets": 3,
            "Liabilities": 1,
            "Equity": 2,
            "Income": 1,
            "Expenses": 1,
        },
        "bean_check_passed": True,
    }
    assert context["accounts"] == {
        "Assets": ["Assets:Bank", "Assets:Broker", "Assets:Savings"],
        "Liabilities": ["Liabilities:Card"],
        "Equity": ["Equity:Opening", "Equity:USD-Opening"],
        "Income": ["Income:Salary"],
        "Expenses": ["Expenses:Food"],
    }
    assert context["prompt_accounts"] == {
        "Income": ["Income:Salary"],
        "Expenses": ["Expenses:Food"],
    }
    assert context["accounts_scope"] == "income_expense"
    assert context["accounts_complete"] is True
    assert context["balance_snapshot"]["scope"] == "nonzero_assets_liabilities_equity"
    assert context["balance_snapshot"]["complete"] is True
    assert context["balance_snapshot"]["accounts"] == [
        {
            "account": "Assets:Bank",
            "positions": [{"number": "220", "commodity": "CNY"}],
        },
        {
            "account": "Assets:Broker",
            "positions": [{"number": "10", "commodity": "USD"}],
        },
        {
            "account": "Assets:Savings",
            "positions": [{"number": "200", "commodity": "CNY"}],
        },
        {
            "account": "Liabilities:Card",
            "positions": [{"number": "500", "commodity": "CNY"}],
        },
        {
            "account": "Equity:USD-Opening",
            "positions": [{"number": "-10", "commodity": "USD"}],
        },
    ]
    flow = context["flow_summary"]
    assert flow["complete_months"][-2] == {
        "month": "2026-05",
        "income": [{"commodity": "CNY", "amount": "1000"}],
        "expenses": [],
    }
    assert flow["complete_months"][-1] == {
        "month": "2026-06",
        "income": [],
        "expenses": [{"commodity": "CNY", "amount": "80"}],
    }
    assert flow["current_partial_month"] == {
        "month": "2026-07",
        "through": "2026-07-16",
        "income": [],
        "expenses": [],
    }


def test_context_emits_empty_months_and_bounded_recent_activity():
    transactions = "\n".join(
        f'2026-07-15 * "Transaction {index}"\n  Expenses:Food {index + 1} CNY\n'
        f"  Assets:Bank {-index - 1} CNY"
        for index in range(20)
    )
    context = _context(
        dedent(
            """
        2025-12-31 open Assets:Bank CNY
        2025-12-31 open Expenses:Food CNY
        """
        )
        + transactions
    )

    assert len(context["recent_activity"]["transactions"]) == MAX_RECENT_TRANSACTIONS
    assert context["recent_activity"]["truncated"] is True
    assert context["recent_activity"]["omitted_transactions"] == 12
    assert context["flow_summary"]["complete_months"][0]["income"] == []
    assert context["flow_summary"]["complete_months"][0]["expenses"] == []


def test_context_groups_only_native_account_types_and_reports_account_truncation():
    opens = "\n".join(
        f"2025-01-01 open Expenses:Category{index} CNY"
        for index in range(MAX_CONTEXT_ACCOUNTS + 10)
    )
    context = _context(opens)

    assert context["accounts_truncated"] is True
    assert context["accounts_omitted"] == 10
    assert context["accounts_scope"] == "income_expense"
    assert context["accounts_complete"] is False
    assert sum(len(accounts) for accounts in context["accounts"].values()) == MAX_CONTEXT_ACCOUNTS
    assert set(context["accounts"]) == {
        "Assets",
        "Liabilities",
        "Equity",
        "Income",
        "Expenses",
    }
    assert all(
        "primary" not in account.lower()
        for accounts in context["accounts"].values()
        for account in accounts
    )


def test_context_recent_raw_text_is_bounded_and_truncation_is_explicit():
    context = _context(
        "2025-01-01 open Assets:Bank CNY\n" + ("; ledger formatting\n" * 1_000)
    )

    recent_text = context["recent_ledger_text"]
    assert len(recent_text["text"]) <= 4_000
    assert recent_text["truncated"] is True
    assert len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))) <= (
        MAX_LEDGER_CONTEXT_CHARS + 32
    )


def test_context_budget_reports_prompt_account_omissions():
    opens = "\n".join(
        f"2025-01-01 open Expenses:Category{index}{'LongName' * 30} CNY"
        for index in range(MAX_CONTEXT_ACCOUNTS)
    )
    context = _context(opens)

    assert context["accounts_truncated"] is True
    assert context["accounts_omitted"] > 0
    assert context["prompt_accounts_truncated"] is True
    assert context["prompt_accounts_omitted"] > 0
    assert context["accounts_complete"] is False
