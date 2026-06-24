"""Deterministic preview evaluator for Tier 1 cases."""

import logging
from decimal import Decimal, InvalidOperation

from .config import DeterministicAssertions, PostingAssertion

logger = logging.getLogger(__name__)


def _normalize_units(units_str: str | None) -> str | None:
    """Normalize a numeric string for comparison (strip trailing zeros, handle negative)."""
    if units_str is None:
        return None
    try:
        d = Decimal(str(units_str))
        return str(d.normalize())
    except InvalidOperation:
        return str(units_str)


class Tier1Result:
    def __init__(self):
        self.passed: bool = True
        self.errors: list[str] = []
        self.score: int = 0
        self.extracted_block: str | None = None

    def fail(self, message: str):
        self.passed = False
        self.errors.append(message)


def _is_marker_entry(entry) -> bool:
    """Return True for meta-entries (commodity, open, option, event, etc.)
    that should not be counted as user-facing entries."""
    from beancount.core.data import Commodity, Open, Close, Event, Query, Note, Document, Pad, Balance, Custom

    return isinstance(entry, (Commodity, Open, Close, Event, Query, Note, Document, Pad, Balance, Custom))


def _is_from_fixture(entry, fixture_content: str | None) -> bool:
    """Heuristic: an entry is from the fixture if it exists in fixture_content.

    Since we can't perfectly attribute entries, we use a simple approach:
    mark entries whose meta['filename'] doesn't match '<string>' (parser-generated
    for the synthetic input) as fixture entries.
    """
    meta_filename = getattr(entry, "meta", {}).get("filename", "")
    if meta_filename and meta_filename != "<string>":
        return True
    return False


def evaluate_tier1(
    beancount_block: str,
    assertions: DeterministicAssertions,
    fixture_content: str | None = None,
) -> Tier1Result:
    """Parse a Beancount code block and validate against deterministic assertions.

    fixture_content provides the full main.beancount (Open directives, existing
    entries) so the parser has context to validate account references.
    """
    result = Tier1Result()
    result.extracted_block = beancount_block

    if not beancount_block or not beancount_block.strip():
        result.fail("No Beancount code block found in response")
        return result

    from beancount import loader

    full_input = (fixture_content or "") + "\n" + beancount_block

    try:
        entries, parse_errors, _ = loader.load_string(full_input)
    except Exception as e:
        result.fail(f"Failed to parse Beancount block: {e}")
        return result

    if parse_errors:
        new_entry_errors = []
        for err in parse_errors:
            err_entry = getattr(err, "entry", None)
            if err_entry is not None and not _is_from_fixture(err_entry, fixture_content):
                new_entry_errors.append(err)
        if new_entry_errors:
            err_msgs = "; ".join(str(e) for e in new_entry_errors)
            result.fail(f"Beancount parse errors (new entry): {err_msgs}")
        elif not any(
            not _is_from_fixture(e, fixture_content)
            for e in entries
            if not _is_marker_entry(e)
        ):
            err_msgs = "; ".join(str(e) for e in parse_errors)
            result.fail(f"Beancount parse errors: {err_msgs}")
            return result

    new_entries = [
        e for e in entries
        if not _is_from_fixture(e, fixture_content) and not _is_marker_entry(e)
    ]
    if not new_entries:
        result.fail("No new entry parsed from Beancount block")
        return result

    entry = new_entries[-1]

    expected_type = assertions.entry_type
    actual_type = _entry_type_name(entry)
    if actual_type != expected_type:
        result.fail(f"Expected entry type '{expected_type}', got '{actual_type}'")

    if assertions.date:
        actual_date_str = str(entry.date)
        if actual_date_str != assertions.date:
            result.fail(f"Expected date '{assertions.date}', got '{actual_date_str}'")

    _check_postings(result, entry, assertions.posting_multiset)
    _check_tags(result, entry, assertions.required_tags)
    _check_links(result, entry, assertions.required_links)
    _check_metadata(result, entry, assertions.required_metadata)
    _check_forbidden_prefixes(result, entry, assertions.forbidden_account_prefixes)
    _check_price_assertion(result, entry, assertions.price_assertion)

    if result.passed:
        result.score = 1

    return result


def _entry_type_name(entry) -> str:
    from beancount.core.data import Transaction, Price

    if isinstance(entry, Transaction):
        return "transaction"
    if isinstance(entry, Price):
        return "price"
    return type(entry).__name__.lower()


def _check_postings(result: Tier1Result, entry, assertions: list[PostingAssertion]):
    if not assertions:
        return
    from beancount.core.data import Transaction

    if not isinstance(entry, Transaction):
        result.fail(f"Expected a Transaction for posting checks, got {type(entry).__name__}")
        return

    actual_postings = []
    for posting in entry.postings:
        units_str = _format_amount(posting.units)
        cost_str = _format_cost(posting.cost)
        price_str = _format_amount(posting.price)
        actual_postings.append({
            "account": posting.account,
            "units": units_str,
            "currency": _currency_from_amount(posting.units),
            "cost": cost_str,
            "unit_price": price_str,
        })

    for assertion in assertions:
        found = False
        for ap in actual_postings:
            if ap["account"] != assertion.account:
                continue
            expected_units = _normalize_units(assertion.units)
            actual_units = _normalize_units(ap["units"])
            if expected_units != actual_units:
                continue
            if assertion.currency and assertion.currency.upper() != (ap.get("currency") or "").upper():
                continue
            if assertion.cost is not None and _normalize_units(assertion.cost) != _normalize_units(ap.get("cost")):
                continue
            if assertion.unit_price is not None and _normalize_units(assertion.unit_price) != _normalize_units(ap.get("unit_price")):
                continue
            found = True
            break

        if not found:
            result.fail(
                f"Missing required posting: account={assertion.account}, "
                f"units={assertion.units}, currency={assertion.currency}"
            )

    expected_count = len(assertions)
    actual_count = len(entry.postings)
    if actual_count != expected_count:
        result.fail(
            f"Posting count mismatch: expected {expected_count}, got {actual_count}"
        )


def _check_tags(result: Tier1Result, entry, required_tags: list[str]):
    if not required_tags:
        return
    from beancount.core.data import Transaction

    if not isinstance(entry, Transaction):
        return

    entry_tags = set(entry.tags or set())
    entry_links = set(entry.links or set())

    for tag in required_tags:
        if isinstance(tag, str):
            if tag in entry_tags or tag in entry_links:
                continue
            result.fail(f"Missing required tag/link: {tag}")


def _check_links(result: Tier1Result, entry, required_links: list[str]):
    if not required_links:
        return
    from beancount.core.data import Transaction

    if not isinstance(entry, Transaction):
        return

    entry_links = set(entry.links or set())
    for link in required_links:
        if link not in entry_links:
            result.fail(f"Missing required link: {link}")


def _check_metadata(result: Tier1Result, entry, required_metadata: dict[str, str]):
    if not required_metadata:
        return
    from beancount.core.data import Transaction

    if not isinstance(entry, Transaction):
        return

    entry_meta = dict(entry.meta or {})
    for key, expected_value in required_metadata.items():
        actual = entry_meta.get(key)
        if actual is None:
            result.fail(f"Missing required metadata key: {key}")
        elif str(actual) != str(expected_value):
            result.fail(
                f"Metadata '{key}' mismatch: expected '{expected_value}', got '{actual}'"
            )


def _check_forbidden_prefixes(result: Tier1Result, entry, forbidden: list[str]):
    if not forbidden:
        return
    from beancount.core.data import Transaction

    if not isinstance(entry, Transaction):
        return

    for posting in entry.postings:
        for prefix in forbidden:
            if posting.account.startswith(prefix):
                result.fail(
                    f"Forbidden account prefix '{prefix}' found in posting '{posting.account}'"
                )


def _check_price_assertion(result: Tier1Result, entry, price_assertion: dict | None):
    if not price_assertion:
        return
    from beancount.core.data import Price

    if not isinstance(entry, Price):
        result.fail(f"Expected Price entry for price assertion, got {type(entry).__name__}")
        return

    expected_commodity = price_assertion.get("commodity")
    expected_amount = _normalize_units(price_assertion.get("amount"))
    expected_currency = price_assertion.get("currency")

    if expected_commodity and entry.currency != expected_commodity:
        result.fail(
            f"Price commodity mismatch: expected '{expected_commodity}', got '{entry.currency}'"
        )

    actual_amount = _normalize_units(str(entry.amount.number))
    if expected_amount and actual_amount != expected_amount:
        result.fail(
            f"Price amount mismatch: expected '{expected_amount}', got '{actual_amount}'"
        )


def _format_amount(amount) -> str:
    if amount is None:
        return ""
    try:
        from decimal import Decimal

        num = amount.number
        if isinstance(num, Decimal):
            return str(num.normalize())
        return str(num)
    except Exception:
        return str(amount)


def _currency_from_amount(amount) -> str:
    if amount is None:
        return ""
    try:
        return str(amount.currency)
    except Exception:
        return ""


def _format_cost(cost) -> str | None:
    if cost is None:
        return None
    try:
        return f"{cost.number} {cost.currency}"
    except Exception:
        return str(cost)
