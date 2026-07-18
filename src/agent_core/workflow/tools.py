import dataclasses
import json as _json_mod
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from agent_core.services import WorkflowToolDependencies


def _dependencies(config: RunnableConfig) -> WorkflowToolDependencies:
    dependencies = config.get("configurable", {}).get("tool_dependencies")
    if not isinstance(dependencies, WorkflowToolDependencies):
        raise RuntimeError("Workflow tool dependencies were not configured for this request")
    return dependencies


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@tool("ledger_find_accounts")
def tool_find_accounts(
    query: str,
    account_type: str = "",
    status: str = "open",
    limit: int = 20,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Resolve a human account hint to exact ledger names and lifecycle facts.

    Use this before a read or approval-gated write when the user supplies a
    label instead of an exact ledger account literal.

    Args:
        query: Non-empty account name, component, or display-name hint.
        account_type: Native root filter: Assets, Liabilities, Equity, Income, or Expenses.
        status: Lifecycle filter: open, closed, or all.
        limit: Maximum candidates; hard-capped at 100.
    """
    c = config.get("configurable", {})
    result = _dependencies(config).queries.find_accounts(
        c.get("workspace", ""),
        query,
        account_type,
        status,
        min(limit, 100),
        c.get("whitelist"),
        c.get("ledger_config"),
    )
    return _json_mod.dumps(dataclasses.asdict(result), ensure_ascii=False)


@tool("ledger_account_balance")
def tool_account_balance(
    account: str,
    as_of_date: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Query one account's balance, optionally as of an ISO date.

    Args:
        account: Exact account name.
        as_of_date: ISO date; empty means latest.
    """
    ws = config.get("configurable", {}).get("workspace", "")
    ledger_config = config.get("configurable", {}).get("ledger_config")
    result = _dependencies(config).queries.get_balance(
        ws, account, as_of_date or None, ledger_config
    )
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_find_transactions")
def tool_find_transactions(
    account: str = "",
    date_from: str = "",
    date_to: str = "",
    narration_contains: str = "",
    limit: int = 20,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Search transactions with optional filters, newest first.

    Args:
        account: Account regex; empty matches all accounts.
        date_from: Inclusive ISO start date.
        date_to: Inclusive ISO end date.
        narration_contains: Narration substring.
        limit: Maximum results; capped at 100.
    """
    ws = config.get("configurable", {}).get("workspace", "")
    ledger_config = config.get("configurable", {}).get("ledger_config")
    result = _dependencies(config).queries.find_transactions(
        ws,
        account or None,
        date_from or None,
        date_to or None,
        narration_contains or None,
        min(limit, 100),
        ledger_config,
    )
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_get_transaction")
def tool_get_transaction(
    transaction_ref: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Return one exact transaction directive and its source facts.

    Args:
        transaction_ref: Opaque reference returned by ledger_find_transactions.
            Do not construct or alter references.
    """
    ws = config.get("configurable", {}).get("workspace", "")
    ledger_config = config.get("configurable", {}).get("ledger_config")
    result = _dependencies(config).queries.get_transaction(
        ws, transaction_ref, ledger_config
    )
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_query")
def tool_query(
    bql: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Run a raw BQL query against the ledger.

    BQL reference:
        columns: date, flag, payee, narration, account, position, balance
        filters: WHERE account ~ "regex", date >= YYYY-MM-DD, date < YYYY-MM-DD
        aggregates: sum(position), count(*), first(date), last(date)
        clauses: GROUP BY, ORDER BY, LIMIT

    Args:
        bql: Complete BQL SELECT statement.
    """
    ws = config.get("configurable", {}).get("workspace", "")
    ledger_config = config.get("configurable", {}).get("ledger_config")
    result = _dependencies(config).queries.query_bql(ws, bql, ledger_config)
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("market_fetch_price")
def tool_market_fetch_price(
    instrument: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Fetch an external market quote without reading or changing the ledger.

    Args:
        instrument: FX pair such as EUR/CNY, or an equity ticker such as AAPL.
    """
    result = _dependencies(config).prices.fetch_market_price(instrument)
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_ingest_file")
def tool_ingest_file(
    file_path: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Read an uploaded UTF-8 text file up to 2 MB.

    Args:
        file_path: Container-local upload path.
    """
    result = _dependencies(config).ingestion.read_file(file_path)
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_run_python")
def tool_run_python(
    code: str,
    input_files: list[str] | None = None,
    stage: bool = False,
    stage_label: str = "import",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Run Python in a sandbox, commonly to parse uploaded transaction files.

    Sandbox constraints:
    - Fresh temporary directory; no ledger or git access.
    - Input files are copied by basename.
    - Standard library and pandas are available; timeout is 60 seconds.
    - stage=False returns stdout inline, capped at 200 KB.
    - stage=True returns a staging_file for ledger_import_transactions plus a
      transaction count and sample, avoiding large output in model context.

    Args:
        code: Python source code to execute.
        input_files: Absolute paths copied into the sandbox.
        stage: Store stdout in a staging file instead of returning it inline.
        stage_label: Short staging filename label.
    """
    result = _dependencies(config).ingestion.run_python(code, input_files, stage, stage_label)
    return _json_mod.dumps(dataclasses.asdict(result))


# ---------------------------------------------------------------------------
# Model-visible mutation tools
# ---------------------------------------------------------------------------


@tool("ledger_prepare_transaction_update")
def tool_ledger_prepare_transaction_update(
    transaction_ref: str,
    revision_fingerprint: str,
    new_transaction_text: str,
    commit_message: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Validate and prepare replacement of one authoritative transaction.

    Args:
        transaction_ref: Unchanged opaque reference returned by
            ledger_find_transactions and ledger_get_transaction.
        revision_fingerprint: Exact fingerprint returned by
            ledger_get_transaction for this directive revision.
        new_transaction_text: Complete replacement Beancount transaction.
        commit_message: Git commit message used if later approved.
    """
    c = config.get("configurable", {})
    ws: str = c.get("workspace", "")
    whitelist = c.get("whitelist")
    ledger_config = c.get("ledger_config")
    result = _dependencies(config).mutations.prepare_transaction_update(
        ws,
        transaction_ref,
        revision_fingerprint,
        new_transaction_text,
        commit_message,
        whitelist,
        ledger_config,
    )
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_prepare_transaction_delete")
def tool_ledger_prepare_transaction_delete(
    transaction_ref: str,
    revision_fingerprint: str,
    commit_message: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Prepare high-risk deletion of one exact sidecar transaction.

    Use only the unchanged reference and revision fingerprint returned by the
    authoritative transaction read tools; never use fuzzy search or replacement.
    """
    c = config.get("configurable", {})
    result = _dependencies(config).mutations.prepare_transaction_delete(
        c.get("workspace", ""),
        transaction_ref,
        revision_fingerprint,
        commit_message,
        c.get("ledger_config"),
    )
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_prepare_price")
def tool_ledger_prepare_price(
    price_date: str,
    base_commodity: str,
    price: str,
    quote_commodity: str,
    source: str,
    effective_at: str,
    commit_message: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Prepare one reviewed native price directive; never fetch a quote."""
    c = config.get("configurable", {})
    result = _dependencies(config).mutations.prepare_price(
        c.get("workspace", ""),
        price_date,
        base_commodity,
        price,
        quote_commodity,
        source,
        effective_at,
        commit_message,
        c.get("ledger_config"),
    )
    return _json_mod.dumps(dataclasses.asdict(result), ensure_ascii=False)


@tool("ledger_import_transactions")
def tool_ledger_import_transactions(
    transactions_text: str = "",
    commit_message: str = "",
    transactions_file: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Validate and prepare a batch import from text or a staged file.

    Args:
        transactions_text: Raw Beancount transaction blocks separated by blank lines.
        commit_message: Git commit message used if later approved.
        transactions_file: Staging file from ledger_run_python(stage=True); use
            instead of transactions_text for large batches.
    """
    c = config.get("configurable", {})
    ws: str = c.get("workspace", "")
    whitelist = c.get("whitelist")
    ledger_config = c.get("ledger_config")
    result = _dependencies(config).mutations.prepare_bulk(
        ws,
        transactions_text,
        commit_message,
        transactions_file or None,
        whitelist,
        ledger_config,
    )
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_open_account")
def tool_ledger_open_account(
    account_name: str,
    currency: str = "",
    open_date: str = "",
    display_name: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Validate and prepare opening a Beancount account for approval.

    Args:
        account_name: Full path beginning with a Beancount root account type.
        currency: Optional commodity constraint; empty means none.
        open_date: ISO account opening date.
        display_name: Optional human-readable metadata label.
    """
    c = config.get("configurable", {})
    ws: str = c.get("workspace", "")
    ledger_config = c.get("ledger_config")
    result = _dependencies(config).mutations.prepare_open(
        ws,
        account_name,
        currency or None,
        open_date,
        display_name or None,
        ledger_config,
    )
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_prepare_account_close")
def tool_ledger_prepare_account_close(
    account_name: str,
    close_date: str,
    commit_message: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Prepare one exact open-account close after lifecycle and balance checks."""
    c = config.get("configurable", {})
    result = _dependencies(config).mutations.prepare_account_close(
        c.get("workspace", ""),
        account_name,
        close_date,
        commit_message,
        c.get("ledger_config"),
    )
    return _json_mod.dumps(dataclasses.asdict(result), ensure_ascii=False)


@tool("ledger_prepare_change_set")
def tool_ledger_prepare_change_set(
    operations: list[dict],
    commit_message: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Validate and prepare ordered, dependent mutations as one change set.

    Supported operations:
      {"type": "open_account", "account_name": "...", "currency": "CNY",
       "open_date": "2026-01-01", "display_name": "..."}
      {"type": "commit_transaction", "transaction_text": "..."}

    Args:
        operations: Ordered open_account and commit_transaction operation objects.
        commit_message: One Git commit message for the whole change set.
    """
    c = config.get("configurable", {})
    ws: str = c.get("workspace", "")
    whitelist = c.get("whitelist")
    ledger_config = c.get("ledger_config")
    result = _dependencies(config).mutations.prepare_change_set(
        ws,
        operations,
        commit_message,
        whitelist,
        ledger_config,
    )
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_calculate_balance_adjustment")
def tool_ledger_calculate_balance_adjustment(
    observed_date: str,
    account: str,
    amount: str,
    currency: str,
    cutoff: str = "end_of_day",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Calculate ledger balance and signed unexplained difference without writing.

    end_of_day includes postings through observed_date and dates a future
    assertion the following day; start_of_day includes only earlier postings.
    """
    c = config.get("configurable", {})
    result = _dependencies(config).mutations.calculate_balance_adjustment(
        c.get("workspace", ""),
        observed_date,
        account,
        amount,
        currency,
        cutoff,
        c.get("ledger_config"),
    )
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_prepare_balance_reconciliation")
def tool_ledger_prepare_balance_reconciliation(
    observed_date: str,
    account: str,
    amount: str,
    currency: str,
    adjustment_account: str,
    cutoff: str = "end_of_day",
    commit_message: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Validate and prepare an explicit balance adjustment and assertion.

    adjustment_account must already exist and is never inferred.
    """
    c = config.get("configurable", {})
    result = _dependencies(config).mutations.prepare_balance_reconciliation(
        c.get("workspace", ""),
        observed_date,
        account,
        amount,
        currency,
        adjustment_account,
        cutoff,
        commit_message,
        c.get("ledger_config"),
    )
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_prepare_balance_update")
def tool_ledger_prepare_balance_update(
    assertion_date: str,
    account: str,
    currency: str,
    adjustment_account: str,
    commit_message: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Validate and prepare an adjustment for a failed balance checkpoint.

    The original transaction and assertion remain unchanged.
    """
    c = config.get("configurable", {})
    result = _dependencies(config).mutations.prepare_balance_update(
        c.get("workspace", ""),
        assertion_date,
        account,
        currency,
        adjustment_account,
        commit_message,
        c.get("ledger_config"),
    )
    return _json_mod.dumps(dataclasses.asdict(result))


# ---------------------------------------------------------------------------
# Tool groups — default model tools.
# ---------------------------------------------------------------------------

TRANSACTION_TOOLS = [
    tool_ledger_prepare_transaction_update,
    tool_ledger_prepare_transaction_delete,
    tool_ledger_prepare_price,
    tool_ledger_import_transactions,
    tool_ledger_open_account,
    tool_ledger_prepare_account_close,
    tool_ledger_prepare_change_set,
    tool_ledger_prepare_balance_reconciliation,
    tool_ledger_prepare_balance_update,
]

ANALYTICS_TOOLS = [
    tool_find_accounts,
    tool_account_balance,
    tool_ledger_calculate_balance_adjustment,
    tool_find_transactions,
    tool_get_transaction,
    tool_query,
    tool_market_fetch_price,
]

INGESTION_TOOLS = [
    tool_ingest_file,
    tool_run_python,
    tool_ledger_import_transactions,
]

CHITCHAT_TOOLS = []

MODEL_TOOLS = [
    *ANALYTICS_TOOLS,
    tool_ingest_file,
    tool_run_python,
    *TRANSACTION_TOOLS,
]
