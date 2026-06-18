"""External market price fetching: exchange rates and stock prices."""

import json
import logging
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_TIMEOUT = 8  # seconds


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "ledger-agent/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def fetch_price(symbol: str) -> str:
    """Fetch a current market price.

    Supports two symbol formats:
      Currency pair  — e.g. "EUR/CNY", "USD/CNY", "HKD/CNY"
      Stock ticker   — e.g. "Microsoft", "SAP.DE", "AAPL"

    For currency pairs, uses the Open Exchange Rates / Frankfurter API (free, no key).
    For stock tickers, uses Yahoo Finance (unofficial JSON endpoint, no key).

    Returns JSON:
      status   SUCCESS | ERROR
      result:
        symbol      normalised symbol string
        price       numeric price
        currency    quote currency (e.g. "CNY" for EUR/CNY)
        source      data source name
    """
    symbol = symbol.strip().upper()

    # ── Currency pair ──────────────────────────────────────────────────────────
    if "/" in symbol:
        base, quote = [s.strip() for s in symbol.split("/", 1)]
        return _fetch_exchange_rate(base, quote)

    # ── Stock ticker ───────────────────────────────────────────────────────────
    return _fetch_stock(symbol)


def _fetch_exchange_rate(base: str, quote: str) -> str:
    """Fetch base/quote exchange rate using the Frankfurter API (ECB data)."""
    url = f"https://api.frankfurter.app/latest?from={base}&to={quote}"
    try:
        data = _get(url)
        rate = data["rates"][quote]
        return json.dumps({
            "status": "SUCCESS",
            "result": {
                "symbol": f"{base}/{quote}",
                "price": rate,
                "currency": quote,
                "source": "Frankfurter (ECB)",
                "date": data.get("date"),
            },
        })
    except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
        logger.warning("Exchange rate fetch failed %s/%s: %s", base, quote, e)
        return json.dumps({
            "status": "ERROR",
            "error": f"Could not fetch {base}/{quote}: {e}",
        })


def _fetch_stock(ticker: str) -> str:
    """Fetch stock price using Yahoo Finance JSON endpoint."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        "?interval=1d&range=1d"
    )
    try:
        data = _get(url)
        meta = data["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        currency = meta.get("currency", "USD")
        return json.dumps({
            "status": "SUCCESS",
            "result": {
                "symbol": ticker,
                "price": price,
                "currency": currency,
                "source": "Yahoo Finance",
                "exchange": meta.get("exchangeName"),
            },
        })
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError) as e:
        logger.warning("Stock price fetch failed %s: %s", ticker, e)
        return json.dumps({
            "status": "ERROR",
            "error": f"Could not fetch price for {ticker}: {e}",
        })
