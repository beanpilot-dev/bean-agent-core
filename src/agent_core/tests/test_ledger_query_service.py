"""Unit tests for LedgerQueryService query-contract compatibility."""

from datetime import date
from pathlib import Path

import pytest

from agent_core.services.beancount import Beancount, LedgerServiceError
from agent_core.services.queries import LedgerQueryService
from agent_core.services.types import LedgerConfig, QueryResult


def test_get_accounts_includes_open_only_accounts(ledger_workspace: Path) -> None:
    sidecar_main = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    sidecar_main.write_text(
        sidecar_main.read_text() + "\n2020-01-01 open Expenses:Tax:Federal USD\n"
    )

    accounts = LedgerQueryService.get_accounts(str(ledger_workspace))

    assert "Expenses:Tax:Federal" in accounts


def test_find_accounts_returns_lifecycle_alias_and_scope_facts(
    ledger_workspace: Path,
) -> None:
    sidecar_main = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    sidecar_main.write_text(
        sidecar_main.read_text()
        + '\n2020-01-02 open Assets:Travel:Card CNY\n  name: "My Travel Card"\n'
        + '\n2020-01-03 open Liabilities:Old:Card USD\n  name: "Travel Card"\n'
        + "2025-01-01 close Liabilities:Old:Card\n"
        + '\n2020-01-04 open Assets:Travel:Wallet CNY\n  name: "旅行卡"\n'
    )
    unrelated = ledger_workspace / "unrelated.beancount"
    unrelated.write_text("2020-01-01 open Assets:Unrelated:Account USD\n")

    result = LedgerQueryService.find_accounts(
        str(ledger_workspace), "Travel Card", status="all", whitelist=["Assets:Travel"]
    )

    assert result.status == "SUCCESS"
    assert result.total == 2
    assert result.truncated is False
    assert [candidate["account_name"] for candidate in result.candidates] == [
        "Liabilities:Old:Card",
        "Assets:Travel:Card",
    ]
    assert result.candidates[0] == {
        "account_name": "Liabilities:Old:Card",
        "match_basis": "exact_display_name",
        "status": "closed",
        "open_date": "2020-01-03",
        "close_date": "2025-01-01",
        "declared_currencies": ["USD"],
        "display_name": "Travel Card",
        "within_conversation_scope": False,
    }
    assert all("Unrelated" not in candidate["account_name"] for candidate in result.candidates)

    unicode_result = LedgerQueryService.find_accounts(
        str(ledger_workspace), "旅行卡", account_type="Assets"
    )
    assert [candidate["account_name"] for candidate in unicode_result.candidates] == [
        "Assets:Travel:Wallet"
    ]


def test_find_accounts_filters_status_and_native_type(ledger_workspace: Path) -> None:
    sidecar_main = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    sidecar_main.write_text(
        sidecar_main.read_text()
        + "\n2020-01-02 open Assets:Travel:Card CNY\n"
        + "2020-01-03 open Liabilities:Old:Card USD\n"
        + "2025-01-01 close Liabilities:Old:Card\n"
    )

    open_assets = LedgerQueryService.find_accounts(
        str(ledger_workspace), "card", account_type="Assets", status="open"
    )
    closed = LedgerQueryService.find_accounts(str(ledger_workspace), "card", status="closed")

    assert [candidate["account_name"] for candidate in open_assets.candidates] == [
        "Assets:Travel:Card"
    ]
    assert [candidate["account_name"] for candidate in closed.candidates] == [
        "Liabilities:Old:Card"
    ]


def test_find_accounts_is_bounded_and_deterministic(ledger_workspace: Path) -> None:
    sidecar_main = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    entries = "".join(
        f'2020-01-01 open Expenses:Travel:Trip{i:03d} CNY\n  name: "Trip {i:03d}"\n'
        for i in range(130)
    )
    sidecar_main.write_text(sidecar_main.read_text() + "\n" + entries)

    first = LedgerQueryService.find_accounts(str(ledger_workspace), "trip", limit=1_000)
    second = LedgerQueryService.find_accounts(str(ledger_workspace), "trip", limit=1_000)

    assert first.count == 100
    assert first.total == 130
    assert first.truncated is True
    assert first.omitted == 30
    assert first.candidates == second.candidates
    assert first.candidates[0]["account_name"] == "Expenses:Travel:Trip000"


def test_find_accounts_rejects_empty_and_invalid_filters(ledger_workspace: Path) -> None:
    empty = LedgerQueryService.find_accounts(str(ledger_workspace), "  ")
    invalid_type = LedgerQueryService.find_accounts(
        str(ledger_workspace), "cash", account_type="Other"
    )
    invalid_status = LedgerQueryService.find_accounts(
        str(ledger_workspace), "cash", status="archived"
    )

    assert empty.status == "ERROR"
    assert invalid_type.status == "ERROR"
    assert invalid_status.status == "ERROR"


def test_read_operations_return_typed_results(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_rows(_workspace: str, bql: str, *_args):
        calls.append(bql)
        if "sum(position)" in bql:
            return [{"balance": "123 CNY"}], None
        return [{"account": "Assets:Cash"}], None

    monkeypatch.setattr(Beancount, "run_bql_rows", fake_rows)
    service = LedgerQueryService()

    balance = service.get_balance(str(ledger_workspace), "Assets:Cash", "2026-06-01")
    found = service.find_transactions(str(ledger_workspace), narration_contains="Lunch")
    queried = service.query_bql(str(ledger_workspace), "SELECT account")

    assert balance.balance == "123 CNY"
    assert found.count == 1
    assert queried.count == 1
    assert all(isinstance(result, QueryResult) for result in (balance, found, queried))
    assert len(calls) == 3


def test_balance_search_and_bql_preserve_contracts_and_config(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = LedgerConfig(
        entry_path="books/root.beancount",
        sidecar_main_path="books/agent_sidecar/main.beancount",
        sidecar_write_dir="books/agent_sidecar",
    )
    calls: list[tuple[str, LedgerConfig | None]] = []

    def fake_rows(_workspace: str, bql: str, received_config: LedgerConfig | None = None):
        calls.append((bql, received_config))
        return [], "broken"

    monkeypatch.setattr(Beancount, "run_bql_rows", fake_rows)

    balance = LedgerQueryService.get_balance(
        str(ledger_workspace), "Assets:Cash", ledger_config=config
    )
    found = LedgerQueryService.find_transactions(str(ledger_workspace), ledger_config=config)
    queried = LedgerQueryService.query_bql(str(ledger_workspace), "SELECT account", config)

    assert balance == QueryResult(status="ERROR", error="broken")
    assert found == QueryResult(status="ERROR", error="broken")
    assert queried == QueryResult(status="ERROR", error="broken", bql="SELECT account")
    assert calls == [
        ('SELECT sum(position) AS balance WHERE account ~ "^Assets:Cash$" ', config),
        (
            "SELECT date, flag, payee, narration, account, position  ORDER BY date DESC LIMIT 20",
            config,
        ),
        ("SELECT account", config),
    ]


def test_find_transactions_preserves_filter_bql_and_result_shape(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_rows(_workspace: str, bql: str, *_args):
        calls.append(bql)
        return [{"narration": "Lunch"}], None

    monkeypatch.setattr(Beancount, "run_bql_rows", fake_rows)

    result = LedgerQueryService.find_transactions(
        str(ledger_workspace),
        account="^Expenses:Food",
        date_from="2026-01-01",
        date_to="2026-01-31",
        narration_contains="Lunch (team)",
        limit=7,
    )

    assert calls == [
        'SELECT date, flag, payee, narration, account, position WHERE account ~ "^Expenses:Food" '
        'AND date >= 2026-01-01 AND date <= 2026-01-31 AND narration ~ "Lunch\\ \\(team\\)" '
        "ORDER BY date DESC LIMIT 7"
    ]
    assert result == QueryResult(
        status="SUCCESS",
        count=1,
        rows=[{"narration": "Lunch"}],
        filters_applied={
            "account": "^Expenses:Food",
            "date_from": "2026-01-01",
            "date_to": "2026-01-31",
            "narration_contains": "Lunch (team)",
            "limit": 7,
        },
    )


def test_queries_use_configured_entry_path(tmp_path: Path) -> None:
    books = tmp_path / "books"
    sidecar = books / "agent_sidecar"
    sidecar.mkdir(parents=True)
    config = LedgerConfig(
        entry_path="books/root.beancount",
        sidecar_main_path="books/agent_sidecar/main.beancount",
        sidecar_write_dir="books/agent_sidecar",
    )
    month = date.today().strftime("%Y-%m")
    (books / "root.beancount").write_text('include "agent_sidecar/main.beancount"\n')
    (sidecar / "main.beancount").write_text(
        "2020-01-01 open Assets:Cash CNY\n"
        "2020-01-01 open Equity:Opening-Balances CNY\n"
        f'include "{month}.beancount"\n'
    )
    (sidecar / f"{month}.beancount").write_text(
        '2020-01-01 * "Opening balance"\n'
        "  Assets:Cash              1000 CNY\n"
        "  Equity:Opening-Balances -1000 CNY\n"
    )

    accounts = LedgerQueryService.get_accounts(str(tmp_path), config)
    balance = LedgerQueryService.get_balance(str(tmp_path), "Assets:Cash", ledger_config=config)

    assert "Assets:Cash" in accounts
    assert "1000 CNY" in (balance.balance or "")


def test_get_accounts_raises_on_bql_error(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Beancount, "run_bql_rows", lambda *_args: ([], "broken"))

    with pytest.raises(LedgerServiceError, match="broken"):
        LedgerQueryService.get_accounts(str(ledger_workspace))
