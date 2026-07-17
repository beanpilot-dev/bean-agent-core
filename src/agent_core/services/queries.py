"""LedgerQueryService — stateless read-only Beancount queries.

This service owns the query surface used by workflow tools and mutation
validation. It never creates sidecar files, validates mutations, or performs
Git operations.
"""

import os
import re
from datetime import date
from typing import Any

from .beancount import Beancount, LedgerServiceError, ParsedLedgerAccount
from .transaction_index import TransactionIndex, parse_transaction_ref
from .types import AccountSearchResult, LedgerConfig, QueryResult

_OPEN_ACCOUNT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}\s+open\s+"
    r"((?:Assets|Liabilities|Equity|Income|Expenses)(?::[A-Za-z][A-Za-z0-9\-]+)+)",
    re.MULTILINE,
)
_ACCOUNT_TYPES = {"Assets", "Liabilities", "Equity", "Income", "Expenses"}
_ACCOUNT_SEARCH_MAX_RESULTS = 100
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _search_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(_TOKEN_RE.findall(_search_text(value)))


def _account_match_basis(account: ParsedLedgerAccount, query: str) -> tuple[int, str] | None:
    normalized_query = _search_text(query)
    query_tokens = _tokens(query)
    name = _search_text(account.account_name)
    display_name = _search_text(account.display_name or "")
    name_components = tuple(_search_text(part) for part in account.account_name.split(":"))
    display_tokens = _tokens(account.display_name or "")
    fields = (name, display_name)

    if normalized_query in fields:
        basis = "exact_account_name" if normalized_query == name else "exact_display_name"
        return 0, basis

    component_tokens = set(name_components) | set(display_tokens)
    if query_tokens and all(token in component_tokens for token in query_tokens):
        return 1, "exact_component"

    if any(field.startswith(normalized_query) for field in fields if field):
        return 2, "prefix"
    if any(
        component.startswith(normalized_query)
        for component in name_components + display_tokens
        if component
    ):
        return 2, "prefix"

    if any(normalized_query in field for field in fields if field):
        return 3, "substring"
    if any(
        normalized_query in component
        for component in name_components + display_tokens
        if component
    ):
        return 3, "substring"
    return None


def _account_candidate(
    account: ParsedLedgerAccount,
    match_basis: str,
    whitelist: list[str] | None,
) -> dict[str, Any]:
    return {
        "account_name": account.account_name,
        "match_basis": match_basis,
        "status": account.status,
        "open_date": account.open_date.isoformat() if account.open_date else None,
        "close_date": account.close_date.isoformat() if account.close_date else None,
        "declared_currencies": list(account.declared_currencies),
        "display_name": account.display_name,
        "within_conversation_scope": not whitelist
        or any(account.account_name.startswith(prefix) for prefix in whitelist),
    }


class LedgerQueryService:
    """Stateless account, balance, transaction-search, and BQL queries."""

    @staticmethod
    def get_accounts(workspace: str, ledger_config: LedgerConfig | None = None) -> list[str]:
        rows, err = Beancount.run_bql_rows(
            workspace, "SELECT DISTINCT account ORDER BY account", ledger_config
        )
        if err:
            raise LedgerServiceError(f"Failed to list accounts: {err}")
        accounts = {r["account"] for r in rows if r.get("account")}
        try:
            for dirpath, dirnames, filenames in os.walk(workspace):
                dirnames[:] = [d for d in dirnames if d not in {".git", ".venv"}]
                for fname in filenames:
                    if not fname.endswith(".beancount"):
                        continue
                    with open(os.path.join(dirpath, fname), encoding="utf-8") as f:
                        accounts.update(_OPEN_ACCOUNT_RE.findall(f.read()))
        except OSError:
            pass
        return sorted(accounts)

    @staticmethod
    def find_accounts(
        workspace: str,
        query: str,
        account_type: str = "",
        status: str = "open",
        limit: int = 20,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> AccountSearchResult:
        """Find exact ledger accounts using parsed lifecycle and display facts."""
        normalized_query = _search_text(query) if isinstance(query, str) else ""
        if not normalized_query:
            return AccountSearchResult(
                status="ERROR",
                query=query if isinstance(query, str) else "",
                account_type=account_type,
                lifecycle_status=status,
                error="query must be non-empty",
            )
        if account_type and account_type not in _ACCOUNT_TYPES:
            return AccountSearchResult(
                status="ERROR",
                query=query,
                account_type=account_type,
                lifecycle_status=status,
                error="account_type must be one of Assets, Liabilities, Equity, Income, Expenses",
            )
        if status not in {"open", "closed", "all"}:
            return AccountSearchResult(
                status="ERROR",
                query=query,
                account_type=account_type,
                lifecycle_status=status,
                error="status must be one of open, closed, all",
            )

        result_limit = min(max(limit, 1), _ACCOUNT_SEARCH_MAX_RESULTS)
        parsed = Beancount.parsed_ledger(workspace, ledger_config)
        matches: list[tuple[int, str, ParsedLedgerAccount, str]] = []
        for account in parsed.account_index:
            if account_type and account.account_name.split(":", 1)[0] != account_type:
                continue
            if status != "all" and account.status != status:
                continue
            match = _account_match_basis(account, normalized_query)
            if match is not None:
                rank, basis = match
                matches.append((rank, account.account_name.casefold(), account, basis))

        matches.sort(key=lambda item: (item[0], item[1], item[2].account_name))
        candidates = [
            _account_candidate(account, basis, whitelist)
            for _rank, _sort_name, account, basis in matches[:result_limit]
        ]
        total = len(matches)
        return AccountSearchResult(
            status="SUCCESS",
            query=query,
            account_type=account_type,
            lifecycle_status=status,
            limit=result_limit,
            candidates=candidates,
            count=len(candidates),
            total=total,
            truncated=total > result_limit,
            omitted=max(0, total - result_limit),
        )

    @staticmethod
    def get_balance(
        workspace: str,
        account: str,
        as_of_date: str | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult:
        date_clause = f"AND date < {as_of_date}" if as_of_date else ""
        bql = f'SELECT sum(position) AS balance WHERE account ~ "^{account}$" {date_clause}'
        rows, error = Beancount.run_bql_rows(workspace, bql, ledger_config)
        if error:
            return QueryResult(status="ERROR", error=error)
        balance_raw = rows[0].get("balance", "").strip() if rows else ""
        return QueryResult(
            status="SUCCESS",
            account=account,
            as_of=as_of_date or "latest",
            balance=balance_raw if balance_raw else "0",
        )

    @staticmethod
    def find_transactions(
        workspace: str,
        account: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        narration_contains: str | None = None,
        limit: int = 20,
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult:
        result_limit = min(max(limit, 1), 100)
        parsed_dates: dict[str, date | None] = {}
        for name, value in (("date_from", date_from), ("date_to", date_to)):
            if value:
                try:
                    parsed_dates[name] = date.fromisoformat(value)
                except ValueError:
                    return QueryResult(
                        status="ERROR",
                        error="date filters must be ISO dates",
                        error_code="INVALID_DATE_FILTER",
                    )
            else:
                parsed_dates[name] = None
        if parsed_dates["date_from"] and parsed_dates["date_to"]:
            if parsed_dates["date_from"] > parsed_dates["date_to"]:
                return QueryResult(
                    status="ERROR",
                    error="date_from must not be after date_to",
                    error_code="INVALID_DATE_RANGE",
                )
        try:
            index = TransactionIndex.build(workspace, ledger_config)
            matches = index.search(
                account=account,
                date_from=parsed_dates["date_from"],
                date_to=parsed_dates["date_to"],
                narration_contains=narration_contains,
            )
        except re.error:
            return QueryResult(
                status="ERROR",
                error="account must be a valid regular expression",
                error_code="INVALID_ACCOUNT_FILTER",
            )
        except LedgerServiceError:
            return QueryResult(
                status="ERROR",
                error="ledger could not be parsed",
                error_code="LEDGER_PARSE_ERROR",
            )
        total = len(matches)
        rows = [match.summary() for match in matches[:result_limit]]
        return QueryResult(
            status="SUCCESS",
            count=len(rows),
            rows=rows,
            total=total,
            truncated=total > result_limit,
            omitted=max(0, total - result_limit),
            filters_applied={
                "account": account,
                "date_from": date_from,
                "date_to": date_to,
                "narration_contains": narration_contains,
                "limit": result_limit,
            },
        )

    @staticmethod
    def get_transaction(
        workspace: str,
        transaction_ref: str,
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult:
        """Resolve one opaque reference against a fresh parser-backed index."""
        if parse_transaction_ref(transaction_ref) is None:
            return QueryResult(
                status="ERROR",
                error="transaction reference is malformed",
                error_code="MALFORMED_TRANSACTION_REF",
            )
        try:
            index = TransactionIndex.build(workspace, ledger_config)
        except LedgerServiceError:
            return QueryResult(
                status="ERROR",
                error="ledger could not be parsed",
                error_code="LEDGER_PARSE_ERROR",
            )
        code, transaction = index.resolve(transaction_ref)
        if transaction is None:
            messages = {
                "MALFORMED_TRANSACTION_REF": "transaction reference is malformed",
                "TRANSACTION_NOT_FOUND": "transaction reference was not found",
                "STALE_TRANSACTION_REF": "transaction reference is stale",
                "AMBIGUOUS_TRANSACTION_REF": "transaction reference is ambiguous",
            }
            return QueryResult(
                status="ERROR",
                error=messages[code],
                error_code=code,
            )
        detail = transaction.detail()
        return QueryResult(
            status="SUCCESS",
            count=1,
            total=1,
            transaction=detail,
            rows=[detail],
            transaction_ref=transaction.transaction_ref,
            directive=transaction.directive,
            source_path=transaction.relative_path,
            source_start_line=transaction.start_line,
            source_end_line=transaction.end_line,
            payee=transaction.facts["payee"],
            narration=transaction.facts["narration"],
            tags=transaction.facts["tags"],
            links=transaction.facts["links"],
            metadata=transaction.facts["metadata"],
            postings=transaction.facts["postings"],
            revision_fingerprint=transaction.revision_fingerprint,
        )

    @staticmethod
    def query_bql(
        workspace: str, bql: str, ledger_config: LedgerConfig | None = None
    ) -> QueryResult:
        rows, error = Beancount.run_bql_rows(workspace, bql, ledger_config)
        if error:
            return QueryResult(status="ERROR", error=error, bql=bql)
        return QueryResult(status="SUCCESS", count=len(rows), rows=rows)
