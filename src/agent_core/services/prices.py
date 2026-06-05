"""PriceService — external market price fetching.

Fetches FX rates (Frankfurter) and stock prices (Yahoo Finance).
No LLM dependency, no API key required.
"""

import json
import logging
import urllib.error
import urllib.request

from .types import PriceResult

logger = logging.getLogger(__name__)

_TIMEOUT = 8


class PriceServiceError(Exception):
    """Unrecoverable price fetch failure."""


class PriceService:
    """External market price data: exchange rates and stock prices."""

    @staticmethod
    def _get(url: str) -> dict:
        req = urllib.request.Request(
            url, headers={"User-Agent": "ledger-agent/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode())

    @staticmethod
    def fetch_price(symbol: str) -> PriceResult:
        symbol = symbol.strip().upper()

        if "/" in symbol:
            base, quote = [s.strip() for s in symbol.split("/", 1)]
            return PriceService._fetch_exchange_rate(base, quote)

        return PriceService._fetch_stock(symbol)

    @staticmethod
    def _fetch_exchange_rate(base: str, quote: str) -> PriceResult:
        url = f"https://api.frankfurter.app/latest?from={base}&to={quote}"
        try:
            data = PriceService._get(url)
            rate = data["rates"][quote]
            return PriceResult(
                status="SUCCESS",
                symbol=f"{base}/{quote}",
                price=rate,
                currency=quote,
                source="Frankfurter (ECB)",
                date=data.get("date"),
            )
        except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
            logger.warning(
                "Exchange rate fetch failed %s/%s: %s", base, quote, e,
            )
            return PriceResult(
                status="ERROR",
                error=f"Could not fetch {base}/{quote}: {e}",
            )

    @staticmethod
    def _fetch_stock(ticker: str) -> PriceResult:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            "?interval=1d&range=1d"
        )
        try:
            data = PriceService._get(url)
            meta = data["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
            currency = meta.get("currency", "USD")
            return PriceResult(
                status="SUCCESS",
                symbol=ticker,
                price=price,
                currency=currency,
                source="Yahoo Finance",
                exchange=meta.get("exchangeName"),
            )
        except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError) as e:
            logger.warning("Stock price fetch failed %s: %s", ticker, e)
            return PriceResult(
                status="ERROR",
                error=f"Could not fetch price for {ticker}: {e}",
            )
