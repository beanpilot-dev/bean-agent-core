"""LedgerQueryService — stateless read-only Beancount queries.

This service owns the query surface used by workflow tools and mutation
validation. It never creates sidecar files, validates mutations, or performs
Git operations.
"""

import os
import re

from .beancount import Beancount, LedgerServiceError
from .types import LedgerConfig, QueryResult

_OPEN_ACCOUNT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}\s+open\s+"
    r"((?:Assets|Liabilities|Equity|Income|Expenses)(?::[A-Za-z][A-Za-z0-9\-]+)+)",
    re.MULTILINE,
)


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
        filters = []
        if account:
            filters.append(f'account ~ "{account}"')
        if date_from:
            filters.append(f"date >= {date_from}")
        if date_to:
            filters.append(f"date <= {date_to}")
        if narration_contains:
            filters.append(f'narration ~ "{re.escape(narration_contains)}"')

        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        bql = (
            f"SELECT date, flag, payee, narration, account, position "
            f"{where} ORDER BY date DESC LIMIT {limit}"
        )
        rows, error = Beancount.run_bql_rows(workspace, bql, ledger_config)
        if error:
            return QueryResult(status="ERROR", error=error)
        return QueryResult(
            status="SUCCESS",
            count=len(rows),
            rows=rows,
            filters_applied={
                "account": account,
                "date_from": date_from,
                "date_to": date_to,
                "narration_contains": narration_contains,
                "limit": limit,
            },
        )

    @staticmethod
    def query_bql(
        workspace: str, bql: str, ledger_config: LedgerConfig | None = None
    ) -> QueryResult:
        rows, error = Beancount.run_bql_rows(workspace, bql, ledger_config)
        if error:
            return QueryResult(status="ERROR", error=error, bql=bql)
        return QueryResult(status="SUCCESS", count=len(rows), rows=rows)

    @staticmethod
    def query_template(
        workspace: str,
        template_name: str,
        params: dict,
        templates_dir: str | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult:
        if templates_dir is None:
            templates_dir = os.path.join(
                os.path.dirname(__file__), "..", "ledger", "query_templates"
            )
        available = sorted(f[:-4] for f in os.listdir(templates_dir) if f.endswith(".bql"))
        if template_name not in available:
            return QueryResult(
                status="ERROR",
                error=f"Unknown template '{template_name}'. Available: {available}",
            )

        template_path = os.path.join(templates_dir, f"{template_name}.bql")
        try:
            with open(template_path) as f:
                lines = [line for line in f if not line.lstrip().startswith("--")]
            bql = "".join(lines).strip()
        except FileNotFoundError:
            return QueryResult(status="ERROR", error=f"Template file not found: {template_name}")

        for key, value in params.items():
            bql = bql.replace(f"{{{key}}}", str(value))

        rows, error = Beancount.run_bql_rows(workspace, bql, ledger_config)
        if error:
            return QueryResult(status="ERROR", error=error, bql=bql)
        return QueryResult(
            status="SUCCESS",
            count=len(rows),
            rows=rows,
            template=template_name,
            params=params,
        )
