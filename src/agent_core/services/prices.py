"""PriceService — external market quote fetching.

Fetches FX rates (Frankfurter) and stock prices (Yahoo Finance).
No LLM dependency, no API key required.
"""

import json
import logging
import math
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from .types import PriceResult

logger = logging.getLogger(__name__)

_TIMEOUT = 8
_FX_CODE = re.compile(r"^[A-Z]{3}$")
_FRANKFURTER = "Frankfurter (ECB)"
_YAHOO_FINANCE = "Yahoo Finance"


class PriceService:
    """External market quote data: exchange rates and equity prices."""

    @staticmethod
    def _get(url: str) -> Any:
        req = urllib.request.Request(
            url, headers={"User-Agent": "ledger-agent/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode())

    @staticmethod
    def fetch_market_price(instrument: str) -> PriceResult:
        if not isinstance(instrument, str):
            return PriceService._error(
                "INVALID_INSTRUMENT", "Market instrument must be a string."
            )
        normalized = instrument.strip().upper()

        if not normalized:
            return PriceService._error(
                "INVALID_INSTRUMENT", "Market instrument must not be empty."
            )

        if "/" in normalized:
            parts = [part.strip() for part in normalized.split("/")]
            if len(parts) != 2 or not all(_FX_CODE.fullmatch(part) for part in parts):
                return PriceService._error(
                    "INVALID_FX_PAIR",
                    "FX instruments must use two ISO currency codes such as EUR/CNY.",
                    normalized,
                )
            base, quote_currency = parts
            return PriceService._fetch_exchange_rate(base, quote_currency)

        return PriceService._fetch_equity(normalized)

    @staticmethod
    def _error(code: str, message: str, instrument: str = "") -> PriceResult:
        return PriceResult(
            status="ERROR",
            instrument=instrument,
            error_code=code,
            error_message=message,
        )

    @staticmethod
    def _numeric_price(value: Any) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        return price if math.isfinite(price) else None

    @staticmethod
    def _fetch_exchange_rate(base: str, quote_currency: str) -> PriceResult:
        instrument = f"{base}/{quote_currency}"
        url = f"https://api.frankfurter.app/latest?from={base}&to={quote_currency}"
        try:
            data = PriceService._get(url)
            if not isinstance(data, dict):
                raise ValueError("response is not an object")
            rates = data.get("rates")
            if not isinstance(rates, dict) or quote_currency not in rates:
                raise ValueError("quote rate is missing")
            rate = PriceService._numeric_price(rates[quote_currency])
            if rate is None:
                raise ValueError("quote rate is not numeric")
            return PriceResult(
                status="SUCCESS",
                instrument=instrument,
                price=rate,
                quote_currency=quote_currency,
                provider=_FRANKFURTER,
                effective_date=data.get("date") if isinstance(data.get("date"), str) else None,
                freshness="daily",
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return PriceService._provider_error(_FRANKFURTER, instrument)
        except (KeyError, TypeError, ValueError):
            return PriceService._invalid_provider_response(_FRANKFURTER, instrument)

    @staticmethod
    def _fetch_equity(ticker: str) -> PriceResult:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker, safe='.-^=')}"
            "?interval=1d&range=1d"
        )
        try:
            data = PriceService._get(url)
            if not isinstance(data, dict):
                raise ValueError("response is not an object")
            chart = data.get("chart")
            results = chart.get("result") if isinstance(chart, dict) else None
            first_result = results[0] if isinstance(results, list) and results else None
            meta = first_result.get("meta") if isinstance(first_result, dict) else None
            if not isinstance(meta, dict):
                raise ValueError("quote metadata is missing")

            regular_price = PriceService._numeric_price(meta.get("regularMarketPrice"))
            previous_close = PriceService._numeric_price(meta.get("previousClose"))
            use_previous_close = regular_price is None
            price = previous_close if use_previous_close else regular_price
            if price is None:
                raise ValueError("quote price is missing or not numeric")

            currency = meta.get("currency", "USD")
            if not isinstance(currency, str) or not currency.strip():
                raise ValueError("quote currency is missing")
            effective_at = PriceService._timestamp(meta.get("regularMarketTime"))
            return PriceResult(
                status="SUCCESS",
                instrument=ticker,
                price=price,
                quote_currency=currency.strip().upper(),
                provider=_YAHOO_FINANCE,
                effective_date=effective_at[:10] if effective_at else None,
                effective_at=effective_at,
                freshness="previous_close" if use_previous_close else "intraday",
                market_state=(
                    meta.get("marketState")
                    if isinstance(meta.get("marketState"), str)
                    else None
                ),
                exchange=meta.get("exchangeName"),
            )
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return PriceService._provider_error(_YAHOO_FINANCE, ticker)
        except (KeyError, IndexError, TypeError, ValueError):
            return PriceService._invalid_provider_response(_YAHOO_FINANCE, ticker)

    @staticmethod
    def _timestamp(value: Any) -> str | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            timestamp = float(value)
            if not math.isfinite(timestamp):
                return None
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            return None

    @staticmethod
    def _provider_error(provider: str, instrument: str) -> PriceResult:
        logger.warning(
            "market quote provider unavailable",
            extra={"provider": provider, "error_code": "PROVIDER_UNAVAILABLE"},
        )
        return PriceService._error(
            "PROVIDER_UNAVAILABLE",
            f"{provider} did not return a market quote for {instrument}.",
            instrument,
        )

    @staticmethod
    def _invalid_provider_response(provider: str, instrument: str) -> PriceResult:
        logger.warning(
            "market quote provider returned an invalid response",
            extra={"provider": provider, "error_code": "PROVIDER_INVALID_RESPONSE"},
        )
        return PriceService._error(
            "PROVIDER_INVALID_RESPONSE",
            f"{provider} returned an unsupported market quote response for {instrument}.",
            instrument,
        )
