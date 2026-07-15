"""Read-only balance reconciliation calculations and checkpoint lookup."""

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from beancount import loader

from .beancount import Beancount, _cfg, _repo_path
from .queries import LedgerQueryService
from .types import InvariantViolation, LedgerConfig, QueryResult, ValidationFailed

_ACCOUNT_NAME_RE = re.compile(
    r"^(Assets|Liabilities|Equity|Income|Expenses)(:[A-Z][A-Za-z0-9\-]+)+$"
)
_CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9\-]*$")
_INVENTORY_AMOUNT_RE = re.compile(
    r"(?P<amount>[-+]?\d[\d,]*(?:\.\d+)?)\s+(?P<currency>[A-Z][A-Z0-9\-]*)"
)


def is_valid_account_name(account: str) -> bool:
    """Return whether an account uses the supported full Beancount shape."""
    return _ACCOUNT_NAME_RE.match(account) is not None


def format_decimal(amount: Decimal) -> str:
    """Render a plain Beancount amount without exponent notation."""
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _parse_single_currency_balance(
    balance: str,
    currency: str,
) -> Decimal | InvariantViolation:
    if balance.strip() in {"", "0"}:
        return Decimal("0")
    positions = [
        (match.group("amount").replace(",", ""), match.group("currency"))
        for match in _INVENTORY_AMOUNT_RE.finditer(balance)
    ]
    if len(positions) != 1:
        return InvariantViolation(
            invariant="RECONCILIATION_MULTI_COMMODITY_BALANCE",
            severity="HARD",
            provided=balance,
            remediation=(
                "Balance reconciliation requires exactly one commodity in the "
                "account balance. Reconcile each commodity separately."
            ),
        )
    raw_amount, actual_currency = positions[0]
    if actual_currency != currency:
        return InvariantViolation(
            invariant="RECONCILIATION_CURRENCY_MISMATCH",
            severity="HARD",
            provided={"requested": currency, "actual": actual_currency},
            remediation="Use the account's balance commodity as the reconciliation currency.",
        )
    try:
        return Decimal(raw_amount)
    except InvalidOperation:
        return InvariantViolation(
            invariant="RECONCILIATION_BALANCE_PARSE",
            severity="HARD",
            provided=balance,
            remediation="Inspect the account balance and retry the reconciliation.",
        )


class ReconciliationCalculator:
    """Inspect balances and assertions without mutating the ledger workspace."""

    def calculate_balance_adjustment(
        self,
        workspace: str,
        observed_date: str,
        account: str,
        amount: str,
        currency: str,
        cutoff: str = "end_of_day",
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult | InvariantViolation | ValidationFailed:
        """Calculate the signed observed-minus-ledger difference at a cutoff."""
        if cutoff not in {"end_of_day", "start_of_day"}:
            return InvariantViolation(
                invariant="RECONCILIATION_CUTOFF",
                severity="HARD",
                provided=cutoff,
                remediation="Use end_of_day or start_of_day for the observed balance cutoff.",
            )
        try:
            parsed_observed_date = date.fromisoformat(observed_date)
        except ValueError:
            return InvariantViolation(
                invariant="RECONCILIATION_DATE_FORMAT",
                severity="HARD",
                provided=observed_date,
                remediation="Provide an ISO date in YYYY-MM-DD format.",
            )
        if not is_valid_account_name(account):
            return InvariantViolation(
                invariant="ACCOUNT_NAME_FORMAT",
                severity="HARD",
                provided=account,
                remediation="Provide a full Beancount account name.",
            )
        if not _CURRENCY_RE.match(currency):
            return InvariantViolation(
                invariant="RECONCILIATION_CURRENCY_FORMAT",
                severity="HARD",
                provided=currency,
                remediation="Provide an uppercase Beancount commodity symbol.",
            )
        try:
            target_amount = Decimal(amount)
        except (InvalidOperation, ValueError):
            return InvariantViolation(
                invariant="RECONCILIATION_AMOUNT_FORMAT",
                severity="HARD",
                provided=amount,
                remediation="Provide a decimal target amount without currency symbols.",
            )

        existing_accounts = set(LedgerQueryService.get_accounts(workspace, ledger_config))
        if account not in existing_accounts:
            return InvariantViolation(
                invariant="ACCOUNT_WHITELIST",
                severity="HARD",
                provided=[account],
                remediation="Open the account before preparing a reconciliation.",
            )
        assertion_date = (
            parsed_observed_date + timedelta(days=1)
            if cutoff == "end_of_day"
            else parsed_observed_date
        ).isoformat()
        balance_result = self.get_balance(workspace, account, assertion_date, ledger_config)
        if balance_result.status != "SUCCESS":
            return ValidationFailed(
                error="reconciliation_balance_query_failed",
                remediation="Resolve the ledger query error and prepare the reconciliation again.",
            )
        current_amount = _parse_single_currency_balance(balance_result.balance or "0", currency)
        if isinstance(current_amount, InvariantViolation):
            return current_amount

        adjustment = target_amount - current_amount
        return QueryResult(
            status="SUCCESS",
            account=account,
            as_of=assertion_date,
            balance=f"{format_decimal(current_amount)} {currency}",
            rows=[
                {
                    "observed_date": observed_date,
                    "cutoff": cutoff,
                    "assertion_date": assertion_date,
                    "ledger_balance": f"{format_decimal(current_amount)} {currency}",
                    "observed_balance": f"{format_decimal(target_amount)} {currency}",
                    "unexplained_difference": f"{format_decimal(adjustment)} {currency}",
                }
            ],
        )

    @staticmethod
    def existing_balance_assertion(
        workspace: str,
        assertion_date: str,
        account: str,
        currency: str,
        ledger_config: LedgerConfig | None = None,
    ) -> Decimal | None:
        """Find a checkpoint only in the active entry file's include graph."""
        config = _cfg(ledger_config)
        try:
            entries, _errors, _options = loader.load_file(
                _repo_path(workspace, config.entry_path)
            )
        except OSError:
            return None
        for entry in entries:
            if (
                entry.__class__.__name__ == "Balance"
                and entry.date.isoformat() == assertion_date
                and entry.account == account
                and entry.amount.currency == currency
            ):
                return Decimal(entry.amount.number)
        return None

    @staticmethod
    def get_balance(
        workspace: str,
        account: str,
        as_of_date: str,
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult:
        """Sum an account and its descendants before an assertion date."""
        account_pattern = re.escape(account)
        bql = (
            f"SELECT sum(position) AS balance "
            f'WHERE account ~ "^{account_pattern}(?::|$)" '
            f"AND date < {as_of_date}"
        )
        rows, error = Beancount.run_bql_rows(workspace, bql, ledger_config)
        if error:
            return QueryResult(status="ERROR", error=error)
        balance_raw = rows[0].get("balance", "").strip() if rows else ""
        return QueryResult(
            status="SUCCESS",
            account=account,
            as_of=as_of_date,
            balance=balance_raw if balance_raw else "0",
        )
