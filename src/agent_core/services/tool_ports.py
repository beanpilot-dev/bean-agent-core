"""Narrow service ports consumed by the LangGraph tool layer.

The workflow owns only LLM-facing schemas. Concrete Beancount, filesystem,
network, and approval implementations are composed per request behind these
protocols and injected through ``RunnableConfig``.
"""

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .approvals.gateway import ToolExecutionGateway
from .ingestion import IngestionService
from .ledger import LedgerService
from .prices import PriceService
from .queries import LedgerQueryService
from .types import (
    FileReadResult,
    LedgerConfig,
    PriceResult,
    QueryResult,
    SandboxResult,
    ServiceResult,
)


class QueryToolPort(Protocol):
    """Read-only ledger operations exposed to workflow tools."""

    def get_balance(
        self,
        workspace: str,
        account: str,
        as_of_date: str | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult: ...

    def find_transactions(
        self,
        workspace: str,
        account: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        narration_contains: str | None = None,
        limit: int = 20,
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult: ...

    def query_bql(
        self,
        workspace: str,
        bql: str,
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult: ...


class IngestionToolPort(Protocol):
    """Uploaded-file and sandbox operations exposed to workflow tools."""

    def read_file(self, file_path: str) -> FileReadResult: ...

    def run_python(
        self,
        code: str,
        input_files: list[str] | None = None,
        stage: bool = False,
        stage_label: str = "import",
    ) -> SandboxResult: ...


class PriceToolPort(Protocol):
    """External price lookup boundary."""

    def fetch_market_price(self, instrument: str) -> PriceResult: ...


class MutationToolPort(Protocol):
    """Approval-gated mutation operations exposed to the model."""

    def prepare_commit(
        self,
        workspace: str,
        transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> ServiceResult: ...

    def prepare_update(
        self,
        workspace: str,
        target_date: str,
        narration: str,
        new_transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> ServiceResult: ...

    def prepare_open(
        self,
        workspace: str,
        account_name: str,
        currency: str | None,
        open_date: str,
        display_name: str | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> ServiceResult: ...

    def prepare_bulk(
        self,
        workspace: str,
        transactions_text: str = "",
        commit_message: str = "",
        transactions_file: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> ServiceResult: ...

    def prepare_change_set(
        self,
        workspace: str,
        operations: list[dict[str, Any]],
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> ServiceResult: ...

    def calculate_balance_adjustment(
        self,
        workspace: str,
        observed_date: str,
        account: str,
        amount: str,
        currency: str,
        cutoff: str = "end_of_day",
        ledger_config: LedgerConfig | None = None,
    ) -> ServiceResult: ...

    def prepare_balance_reconciliation(
        self,
        workspace: str,
        observed_date: str,
        account: str,
        amount: str,
        currency: str,
        adjustment_account: str,
        cutoff: str = "end_of_day",
        commit_message: str = "",
        ledger_config: LedgerConfig | None = None,
    ) -> ServiceResult: ...

    def prepare_balance_update(
        self,
        workspace: str,
        assertion_date: str,
        account: str,
        currency: str,
        adjustment_account: str,
        commit_message: str = "",
        ledger_config: LedgerConfig | None = None,
    ) -> ServiceResult: ...


@dataclass(frozen=True)
class WorkflowToolDependencies:
    """Request-scoped workflow dependency bundle."""

    queries: QueryToolPort
    ingestion: IngestionToolPort
    prices: PriceToolPort
    mutations: MutationToolPort


WorkflowToolDependenciesFactory = Callable[[], WorkflowToolDependencies]


class ServiceQueryToolAdapter:
    """Adapt focused query services to the workflow query port."""

    def __init__(self, queries: LedgerQueryService | None = None) -> None:
        self._queries = queries or LedgerQueryService()

    def get_balance(
        self,
        workspace: str,
        account: str,
        as_of_date: str | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult:
        return self._queries.get_balance(workspace, account, as_of_date, ledger_config)

    def find_transactions(
        self,
        workspace: str,
        account: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        narration_contains: str | None = None,
        limit: int = 20,
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult:
        return self._queries.find_transactions(
            workspace,
            account,
            date_from,
            date_to,
            narration_contains,
            limit,
            ledger_config,
        )

    def query_bql(
        self,
        workspace: str,
        bql: str,
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult:
        return self._queries.query_bql(workspace, bql, ledger_config)


def create_workflow_tool_dependencies() -> WorkflowToolDependencies:
    """Compose fresh concrete tool services for one agent request."""

    ledger = LedgerService()
    return WorkflowToolDependencies(
        queries=ServiceQueryToolAdapter(),
        ingestion=IngestionService(),
        prices=PriceService(),
        mutations=ToolExecutionGateway(ledger),
    )
