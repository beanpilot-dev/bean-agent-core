"""Unit tests for preflight and price services."""

import urllib.error
from pathlib import Path

import pytest

from agent_core.services.preflight import PreflightService, SetupRequiredError
from agent_core.services.prices import PriceService


def test_preflight_validate_and_account_helpers(ledger_workspace: Path) -> None:
    result = PreflightService.validate(str(ledger_workspace))

    assert result.status == "CLEAN"
    assert result.ledger_meta is not None
    assert result.ledger_meta["bean_check_passed"] is True
    assert set(result.accounts_by_type) == {
        "Assets",
        "Liabilities",
        "Equity",
        "Income",
        "Expenses",
    }
    assert set(result.prompt_accounts) == {"Income", "Expenses"}
    assert result.accounts_scope == "income_expense"
    assert result.accounts_complete is True
    assert result.balance_snapshot is not None
    assert result.flow_summary is not None
    assert result.recent_activity is not None
    assert result.recent_ledger_text is not None
    assert {
        "validation",
        "account_extraction",
        "ledger_metadata",
        "balance_snapshot",
        "flow_summary",
        "recent_context",
    } <= set(result.timings_ms)
    assert "Assets:Cash" in PreflightService.list_accounts(str(ledger_workspace))
    assert any(
        "open Assets:Cash" in line
        for line in PreflightService.get_raw_open_directives(str(ledger_workspace))
    )


def test_preflight_is_read_only_when_the_monthly_sidecar_is_absent(
    ledger_workspace: Path,
) -> None:
    sidecar = ledger_workspace / "data" / "agent_inc"
    monthly_files = list(sidecar.glob("20??-??.beancount"))
    for path in monthly_files:
        path.unlink()
    before = (sidecar / "main.beancount").read_text()

    result = PreflightService.validate(str(ledger_workspace))

    assert result.status == "CLEAN"
    assert not list(sidecar.glob("20??-??.beancount"))
    assert (sidecar / "main.beancount").read_text() == before


def test_preflight_missing_sidecar_raises(ledger_workspace: Path) -> None:
    main = ledger_workspace / "data" / "main.beancount"
    main.write_text('option "title" "Missing sidecar"\n')

    with pytest.raises(SetupRequiredError, match="Sidecar include"):
        PreflightService.validate(str(ledger_workspace))


def test_price_service_fetches_fx_and_equity_quotes(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            {"rates": {"CNY": 7.8}, "date": "2026-06-15"},
            {"chart": {"result": [{"meta": {"regularMarketPrice": 200.0, "currency": "USD"}}]}},
        ]
    )
    monkeypatch.setattr(PriceService, "_get", lambda _url: next(responses))

    fx = PriceService.fetch_market_price("eur/cny")
    stock = PriceService.fetch_market_price("aapl")

    assert (fx.status, fx.instrument, fx.price) == ("SUCCESS", "EUR/CNY", 7.8)
    assert (fx.quote_currency, fx.provider, fx.effective_date, fx.freshness) == (
        "CNY",
        "Frankfurter (ECB)",
        "2026-06-15",
        "daily",
    )
    assert (stock.status, stock.instrument, stock.price) == ("SUCCESS", "AAPL", 200.0)
    assert stock.quote_currency == "USD"
    assert stock.provider == "Yahoo Finance"
    assert stock.freshness == "intraday"


@pytest.mark.parametrize(
    "symbol,payload",
    [
        ("EUR/CNY", {}),
        ("AAPL", {"chart": {"result": []}}),
        ("AAPL", {"chart": {"result": [None]}}),
        ("AAPL", {"chart": {"result": [{"meta": {"regularMarketPrice": "n/a"}}]}}),
    ],
)
def test_price_service_handles_api_failures(
    symbol: str, payload: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PriceService, "_get", lambda _url: payload)

    result = PriceService.fetch_market_price(symbol)

    assert result.status == "ERROR"
    assert result.error_code == "PROVIDER_INVALID_RESPONSE"
    assert result.error_message


def test_price_service_handles_network_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_url: str):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(PriceService, "_get", fail)
    result = PriceService.fetch_market_price("AAPL")
    assert result.status == "ERROR"
    assert result.error_code == "PROVIDER_UNAVAILABLE"


@pytest.mark.parametrize(
    "instrument,error_code",
    [("", "INVALID_INSTRUMENT"), (" / ", "INVALID_FX_PAIR"), ("EUR/CNY/USD", "INVALID_FX_PAIR")],
)
def test_price_service_rejects_invalid_instruments(instrument: str, error_code: str) -> None:
    result = PriceService.fetch_market_price(instrument)

    assert result.status == "ERROR"
    assert result.error_code == error_code
    assert result.error_message


def test_price_service_exposes_equity_effective_timestamp_and_market_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        PriceService,
        "_get",
        lambda _url: {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "regularMarketPrice": 200.0,
                            "currency": "usd",
                            "regularMarketTime": 1781524800,
                            "marketState": "REGULAR",
                            "exchangeName": "NMS",
                        }
                    }
                ]
            }
        },
    )

    result = PriceService.fetch_market_price("aapl")

    assert result.effective_at == "2026-06-15T12:00:00+00:00"
    assert result.effective_date == "2026-06-15"
    assert result.market_state == "REGULAR"
    assert result.exchange == "NMS"


def test_price_service_falls_back_to_previous_close_when_market_price_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        PriceService,
        "_get",
        lambda _url: {
            "chart": {"result": [{"meta": {"regularMarketPrice": None, "previousClose": 199.5}}]}
        },
    )

    result = PriceService.fetch_market_price("AAPL")

    assert result.status == "SUCCESS"
    assert result.price == 199.5
    assert result.freshness == "previous_close"
