"""Deterministic Beancount price identity and validation helpers."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from beancount import loader

from .beancount import _cfg, _repo_path
from .types import LedgerConfig

_COMMODITY_RE = re.compile(r"^[A-Z][A-Z0-9'._-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class PriceIdentity:
    price_date: str
    base_commodity: str
    quote_commodity: str
    value: str

    def key(self) -> tuple[str, str, str]:
        return self.price_date, self.base_commodity, self.quote_commodity


def parse_price_identity(
    price_date: str,
    base_commodity: str,
    price: str,
    quote_commodity: str,
) -> tuple[PriceIdentity | None, str | None]:
    if not isinstance(price_date, str) or not _DATE_RE.fullmatch(price_date):
        return None, "price_date must be an ISO date (YYYY-MM-DD)."
    try:
        date.fromisoformat(price_date)
    except ValueError:
        return None, "price_date must be a valid ISO date."
    for name, commodity in (
        ("base_commodity", base_commodity),
        ("quote_commodity", quote_commodity),
    ):
        if not isinstance(commodity, str) or not _COMMODITY_RE.fullmatch(commodity):
            return None, f"{name} must be an uppercase Beancount commodity."
    try:
        value = Decimal(str(price).strip())
    except (InvalidOperation, ValueError):
        return None, "price must be a finite positive decimal."
    if not value.is_finite() or value <= 0:
        return None, "price must be a finite positive decimal."
    normalized = format(value, "f").rstrip("0").rstrip(".") or "0"
    return PriceIdentity(price_date, base_commodity, quote_commodity, normalized), None


def validate_source(source: str) -> str | None:
    if not isinstance(source, str) or not source.strip() or len(source.strip()) > 500:
        return "source must be a non-empty value of at most 500 characters."
    if any(char in source for char in "\r\n"):
        return "source must not contain line breaks."
    return None


def normalize_effective_at(effective_at: str) -> tuple[str | None, str | None]:
    if not isinstance(effective_at, str) or not effective_at.strip():
        return None, "effective_at must be an ISO-8601 timestamp."
    value = effective_at.strip()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, "effective_at must be an ISO-8601 timestamp."
    if parsed.tzinfo is None:
        return None, "effective_at must include a timezone."
    return parsed.isoformat(), None


def _price_entries(workspace: str, config: LedgerConfig | None = None) -> list[PriceIdentity]:
    cfg = _cfg(config)
    entries, _errors, _options = loader.load_file(_repo_path(workspace, cfg.entry_path))
    prices: list[PriceIdentity] = []
    for entry in entries:
        if entry.__class__.__name__ != "Price":
            continue
        amount = getattr(entry, "amount", None)
        base = getattr(entry, "currency", None)
        quote = getattr(amount, "currency", None)
        if not isinstance(base, str) or not isinstance(quote, str) or amount is None:
            continue
        value = getattr(amount, "number", None)
        identity, error = parse_price_identity(
            entry.date.isoformat(), base, str(value), quote
        )
        if identity is not None and error is None:
            prices.append(identity)
    return prices


def price_state(
    workspace: str,
    identity: PriceIdentity,
    config: LedgerConfig | None = None,
) -> str:
    matching = [
        price.value
        for price in _price_entries(workspace, config)
        if price.key() == identity.key()
    ]
    if not matching:
        return "absent"
    if identity.value in matching and all(value == identity.value for value in matching):
        return f"exact:{identity.value}"
    return "conflict:" + ",".join(sorted(set(matching)))


def price_state_digest(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def price_fact_subject(identity: PriceIdentity) -> str:
    return json.dumps(
        {
            "date": identity.price_date,
            "base": identity.base_commodity,
            "quote": identity.quote_commodity,
            "value": identity.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
