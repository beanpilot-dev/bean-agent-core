import dataclasses
import json as _json_mod
from typing import Annotated

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from agent_core.ledger import analytics, report
from agent_core.services import (
    IngestionService,
    LedgerQueryService,
    LedgerService,
    PriceService,
    ToolExecutionGateway,
)

_ledger = LedgerService()
_queries = LedgerQueryService()
_gateway = ToolExecutionGateway(_ledger)
_prices = PriceService()
_ingestion = IngestionService()


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@tool("ledger_preflight")
def tool_preflight(config: Annotated[RunnableConfig, InjectedToolArg] = None) -> str:  # pyright: ignore[reportArgumentType]
    """Refresh the deterministic preflight check on the Beancount ledger.
    Returns STATUS (CLEAN or ERROR), TARGET file path,
    valid ACCOUNTS list, and RECENT transactions."""
    c = config.get("configurable", {})
    ws: str = c.get("workspace", "")
    ledger_config = c.get("ledger_config")
    result = _ledger.preflight_report(ws, ledger_config)
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_account_balance")
def tool_account_balance(
    account: str,
    as_of_date: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Query the current balance of a specific account.

    Args:
        account: Exact account name (e.g. 'Assets:Liquid:Bank:Checking').
        as_of_date: Optional ISO date to get balance as of that date (e.g. '2026-03-31').
                    Defaults to latest if not provided.

    Returns a JSON string with status SUCCESS or DEPENDENCY_UNAVAILABLE,
    and balance containing the amount(s).
    """
    ws = config.get("configurable", {}).get("workspace", "")
    ledger_config = config.get("configurable", {}).get("ledger_config")
    result = _queries.get_balance(ws, account, as_of_date or None, ledger_config)
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
    """Search for transactions matching one or more filters.

    All parameters are optional — combine freely.

    Args:
        account: Filter by account name (regex-matched, e.g. 'Assets:Liquid:Bank').
        date_from: Start date inclusive (e.g. '2026-01-01').
        date_to: End date inclusive (e.g. '2026-03-31').
        narration_contains: Substring to match in transaction narration.
        limit: Maximum number of results (default 20, max 100).

    Returns a JSON string with status SUCCESS and rows containing
    matched transactions ordered by date descending.
    """
    ws = config.get("configurable", {}).get("workspace", "")
    ledger_config = config.get("configurable", {}).get("ledger_config")
    result = _queries.find_transactions(
        ws,
        account or None,
        date_from or None,
        date_to or None,
        narration_contains or None,
        min(limit, 100),
        ledger_config,
    )
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_query_template")
def tool_query_template(
    template_name: str,
    params: dict,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Execute a named BQL query template for financial analysis.

    Prefer this over ledger_query when the analysis fits a standard pattern —
    it guarantees correct BQL syntax and is faster to invoke.

    Available templates:
        spending_breakdown      Totals and count by account sub-category
                                params: account_pattern, start, end
        spending_trend          Monthly total over time (for trend analysis)
                                params: account_pattern, start, end
        transaction_frequency   Monthly count + total (detects high-freq habits)
                                params: account_pattern, start, end
        large_transactions      Top N individual transactions by size
                                params: account_pattern, start, end, limit
        account_snapshot        Current balance by account (no date filter)
                                params: account_pattern
        period_total            Single aggregate sum for a period
                                params: account_pattern, start, end
        account_total           Single aggregate sum, all time (no date filter)
                                params: account_pattern
        narration_search        Search by keyword when accounts lack granularity
                                params: keyword, account_pattern, start, end, limit
        savings_monthly         Income + expenses by month for savings rate
                                params: start, end

    Param conventions:
        account_pattern  BQL regex, e.g. "^Expenses:Food" or "^(Assets:|Liabilities:)"
        start / end      ISO date strings, end is exclusive, e.g. "2026-01-01" / "2026-02-01"
        limit            integer, default 10

    Note on income: Income accounts hold negative values in Beancount.
    Negate the result numbers to get positive income figures when interpreting.

    Args:
        template_name: One of the template names listed above.
        params: Dict of parameter values to substitute into the template.

    Returns JSON with status SUCCESS (rows) or ERROR (error + bql that failed).
    """
    ws = config.get("configurable", {}).get("workspace", "")
    ledger_config = config.get("configurable", {}).get("ledger_config")
    result = _queries.query_template(ws, template_name, params, ledger_config=ledger_config)
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_query")
def tool_query(
    bql: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Execute a BQL (Bean Query Language) query against the ledger for financial analysis.

    Use this as a last resort when no ledger_query_template fits. Prefer templates
    for standard patterns (trends, breakdowns, snapshots, frequency, search).

    BQL is a SQL-like language for querying Beancount data. Use this to answer any
    financial question: spending patterns, income trends, category breakdowns,
    balance history, large transaction detection, period comparisons, etc.

    BQL reference:
        Columns:  date, flag, payee, narration, account, position, balance
        Filters:  WHERE account ~ "regex"  |  date >= YYYY-MM-DD  |  date < YYYY-MM-DD
        Aggregate: sum(position), count(*), first(date), last(date)
        Grouping:  GROUP BY account  |  GROUP BY year, month
        Ordering:  ORDER BY date DESC  |  ORDER BY sum(position)
        Limit:     LIMIT 50

    Analysis patterns:
        Spending by category (current year):
            SELECT account, sum(position) AS total
            WHERE account ~ "^Expenses" AND date >= 2026-01-01
            GROUP BY account ORDER BY sum(position)

        Monthly income vs expenses trend:
            SELECT year, month, account, sum(position) AS total
            WHERE account ~ "^(Income|Expenses)"
            GROUP BY year, month, account ORDER BY year, month

        Large single transactions:
            SELECT date, payee, narration, position
            WHERE account ~ "^Expenses" ORDER BY position DESC LIMIT 20

        Asset snapshot:
            SELECT account, sum(position) AS balance
            WHERE account ~ "^Assets" GROUP BY account

    Args:
        bql: A complete BQL SELECT statement.

    Returns JSON with status SUCCESS (rows) or ERROR (error message).
    """
    ws = config.get("configurable", {}).get("workspace", "")
    ledger_config = config.get("configurable", {}).get("ledger_config")
    result = _queries.query_bql(ws, bql, ledger_config)
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_query_report")
def tool_query_report(
    year: int = 0,
    month: int = 0,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Generate the full HTML monthly financial report file.
    Runs all queries and renders the dark-theme dashboard with charts,
    savings goal progress, MoM comparisons, and per-account breakdowns.

    Args:
        year: Year to report (e.g. 2026). Defaults to current year.
        month: Month to report as integer (e.g. 3 for March). Defaults to current month.

    Returns the absolute path to the generated HTML report file."""
    cfg = config.get("configurable", {})
    ws = cfg.get("workspace", "")
    ledger_config = cfg.get("ledger_config")
    entry_path = getattr(ledger_config, "entry_path", "data/main.beancount")
    return report.run(ws, analytics.run(ws, year, month, entry_path))


@tool("ledger_fetch_price")
def tool_fetch_price(symbol: str) -> str:
    """Fetch a current market price for a currency pair or stock ticker.

    Supports two formats:
      Currency pair — e.g. "EUR/CNY", "USD/CNY", "HKD/CNY"
                      Uses the Frankfurter API (ECB data, free, no key needed).
      Stock ticker  — e.g. "Microsoft", "AAPL", "0700.HK"
                      Uses the Yahoo Finance JSON endpoint (free, no key needed).

    Use this to record ESPP valuations, foreign transactions, or to answer
    questions like "what is the Microsoft share price today?" before recording
    a cost-basis entry.

    Args:
        symbol: Currency pair (e.g. "EUR/CNY") or stock ticker (e.g. "Microsoft").

    Returns a JSON string with status SUCCESS (result.symbol, result.price,
    result.currency, result.source) or ERROR.
    """
    result = _prices.fetch_price(symbol)
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_ingest_file")
def tool_ingest_file(file_path: str) -> str:
    """Read a file and return its text content for processing.

    Use this to inspect an uploaded bank export (CSV, TSV, plain text) before
    running ledger_run_python to parse it into beancount transactions.

    Upload files via POST /upload first — it returns the container-local path
    to pass here.

    Supports any UTF-8 text file up to 2 MB.

    Args:
        file_path: Container-local path to the file (e.g. '/tmp/a2a_uploads/export.csv_abc123.csv').

    Returns a JSON string with status SUCCESS (result.content, result.lines,
    result.size_bytes) or ERROR.
    """
    result = _ingestion.read_file(file_path)
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_run_python")
def tool_run_python(
    code: str,
    input_files: list[str] | None = None,
    stage: bool = False,
    stage_label: str = "import",
) -> str:
    """Run a Python script in a sandboxed subprocess and return its stdout.

    Primary use case: parse a bank CSV export and print beancount transaction
    blocks to stdout, which the agent then validates with ledger_import_transactions.

    Sandbox constraints:
    - Fresh temp directory per run; no access to the ledger workspace or git.
    - Input files (if given) are copied into the sandbox by their basename.
    - Standard library + pandas, csv, json, re, datetime available.
    - Hard timeout 60 seconds.

    Two output modes:

    stage=False (default) — stdout returned inline (max 200 KB).
      Use for small batches or when you need to inspect the full output.

    stage=True — stdout written to a /tmp staging file; only metadata returned.
      result.staging_file   path to pass to ledger_import_transactions(transactions_file=...)
      result.transaction_count  total parsed
      result.sample         first 5 transaction headers
      Use for large batches (50+ transactions) — the full text never enters LLM
      context, which keeps token cost low and avoids truncation.

    Typical workflow for large batch import:
        1. ledger_ingest_file(path)                        — inspect columns
        2. ledger_run_python(code, [path], stage=True)      — parse + stage
        3. ledger_import_transactions(transactions_file=..., msg)  — approval request

    Example script:

        import csv
        with open("export.csv") as f:
            for row in csv.DictReader(f):
                date = row["Date"]
                narration = row["Note"]
                amount = float(row["Amount"])
                if amount < 0:
                    print(f'{date} * "" "{narration}"')
                    print(f"  Liabilities:CMB-Credit  {amount:.2f} CNY")
                    print(f"  Expenses:Unknown        {-amount:.2f} CNY")
                    print()

    Args:
        code: Python source code to execute.
        input_files: Absolute paths to files copied into the sandbox by basename.
        stage: If True, write stdout to /tmp and return staging_file path + metadata.
        stage_label: Short label embedded in the staging filename (e.g. "cmb_april").

    Returns JSON with status SUCCESS and either:
      stage=False: result.stdout, result.stderr, result.exit_code
      stage=True:  result.staging_file, result.transaction_count, result.sample,
                   result.stderr, result.exit_code
    or ERROR.
    """
    result = _ingestion.run_python(code, input_files, stage, stage_label)
    return _json_mod.dumps(dataclasses.asdict(result))


# ---------------------------------------------------------------------------
# Write tools — model-visible mutation intents plus execution-only confirm functions
# ---------------------------------------------------------------------------


@tool("ledger_commit_transaction")
def tool_ledger_commit_transaction(
    transaction_text: str,
    commit_message: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Validate a transaction mutation and return an approval-required outcome.

    This looks like a write tool, but it never performs a durable ledger write.
    It validates accounts, runs an isolated dry-run bean-check, and returns a
    signed/digested pending action for explicit user approval.

    Args:
        transaction_text: The complete beancount transaction text.
        commit_message: Git commit message for the later approved apply step.

    Returns a JSON string. Possible statuses:
        approval_required — dry-run validated; host/user approval is required
        repairable_error  — revise the draft and retry the mutation tool
    """
    c = config.get("configurable", {})
    ws: str = c.get("workspace", "")
    whitelist = c.get("whitelist")
    ledger_config = c.get("ledger_config")
    result = _gateway.prepare_commit(ws, transaction_text, commit_message, whitelist, ledger_config)
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_update_transaction")
def tool_ledger_update_transaction(
    date: str,
    narration: str,
    new_transaction_text: str,
    commit_message: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Validate a transaction replacement and return an approval-required outcome.

    Workflow:
    1. Use ledger_find_transactions to locate the entry and confirm which one to edit.
    2. Call this tool with the replacement text.
    3. Show the returned approval-required action to the user.

    Args:
        date: Transaction date in ISO format (e.g. '2026-04-10').
        narration: Substring of the narration or payee that uniquely identifies it.
        new_transaction_text: The complete replacement Beancount transaction text.
        commit_message: Git commit message for the later approved apply step.

    Returns a JSON string. Possible statuses:
        approval_required — replacement dry-run validated; approval is required
        repairable_error  — revise the replacement or search target and retry
    """
    c = config.get("configurable", {})
    ws: str = c.get("workspace", "")
    whitelist = c.get("whitelist")
    ledger_config = c.get("ledger_config")
    result = _gateway.prepare_update(
        ws,
        date,
        narration,
        new_transaction_text,
        commit_message,
        whitelist,
        ledger_config,
    )
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_import_transactions")
def tool_ledger_import_transactions(
    transactions_text: str = "",
    commit_message: str = "",
    transactions_file: str = "",
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Validate a batch import and return one approval-required outcome.

    Two input modes:
    - transactions_text: raw Beancount transaction blocks for small batches.
    - transactions_file: staging file path returned by ledger_run_python(stage=True).

    The tool reads staged text when provided, validates accounts, runs isolated
    bean-check, and returns a pending action. It does not append, commit, or push.

    Args:
        transactions_text: Raw Beancount transaction blocks separated by blank lines.
        commit_message: Git commit message for the later approved apply step.
        transactions_file: Optional /tmp staging file from ledger_run_python(stage=True).

    Returns a JSON string. Possible statuses:
        approval_required — batch dry-run validated; approval is required
        repairable_error  — revise the batch input and retry
    """
    c = config.get("configurable", {})
    ws: str = c.get("workspace", "")
    whitelist = c.get("whitelist")
    ledger_config = c.get("ledger_config")
    result = _gateway.prepare_bulk(
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
    """Validate opening a Beancount account and return an approval-required outcome.

    Use this when the user explicitly asks to create an account, or when a
    requested ledger mutation needs a new account that does not yet exist.
    This tool never performs a durable ledger write. It validates the account
    directive, runs isolated dry-run bean-check, and returns a pending action
    for explicit user approval.

    Args:
        account_name: Full Beancount account path (e.g. 'Assets:Liquid:Bank:NewBank').
                      Must start with Assets, Liabilities, Equity, Income, or Expenses.
        currency: Optional commodity constraint (e.g. 'USD'). Pass empty string for none.
        open_date: ISO date when the account should be opened (e.g. '2026-01-01').
        display_name: Optional human-readable label stored as account metadata.

    Returns a JSON string. Possible statuses:
        approval_required — account directive dry-run validated; approval is required
        repairable_error  — revise the account details and retry
    """
    c = config.get("configurable", {})
    ws: str = c.get("workspace", "")
    ledger_config = c.get("ledger_config")
    result = _gateway.prepare_open(
        ws,
        account_name,
        currency or None,
        open_date,
        display_name or None,
        ledger_config,
    )
    return _json_mod.dumps(dataclasses.asdict(result))


@tool("ledger_prepare_change_set")
def tool_ledger_prepare_change_set(
    operations: list[dict],
    commit_message: str,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,  # pyright: ignore[reportArgumentType]
) -> str:
    """Validate related ledger mutations as one approval-required change set.

    Use this when a user-approved transaction depends on a new account in the
    same request. The tool replays operations in order in one isolated dry-run,
    so a later transaction can use an account opened by an earlier operation.
    It never performs a durable ledger write.

    Supported operations:
      {"type": "open_account", "account_name": "...", "currency": "CNY",
       "open_date": "2026-01-01", "display_name": "..."}
      {"type": "commit_transaction", "transaction_text": "..."}

    Args:
        operations: Ordered open_account and commit_transaction operation objects.
        commit_message: One Git commit message for the whole approved change set.

    Returns a JSON string. Possible statuses:
        approval_required — ordered dry-run validated; approval is required
        repairable_error  — revise the operation at the reported index and retry
    """
    c = config.get("configurable", {})
    ws: str = c.get("workspace", "")
    whitelist = c.get("whitelist")
    ledger_config = c.get("ledger_config")
    result = _gateway.prepare_change_set(
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
    """Calculate the ledger balance and signed unexplained difference without writing.

    observed_date and cutoff define the observation precisely. End-of-day is the
    default: postings through observed_date are included and the assertion would
    be dated the following day. Start-of-day includes only earlier postings.
    """
    c = config.get("configurable", {})
    result = _gateway.calculate_balance_adjustment(
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
    """Prepare an approval-gated explicit balance adjustment and assertion.

    adjustment_account must already exist and is never inferred. This tool never
    writes until the user approves the exact generated transaction and assertion.
    """
    c = config.get("configurable", {})
    result = _gateway.prepare_balance_reconciliation(
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
    """Prepare an explicit adjustment for an existing failed balance checkpoint.

    The original transaction and assertion remain unchanged. The user must review
    and approve the new adjustment transaction.
    """
    c = config.get("configurable", {})
    result = _gateway.prepare_balance_update(
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
    tool_ledger_commit_transaction,
    tool_ledger_update_transaction,
    tool_ledger_import_transactions,
    tool_ledger_open_account,
    tool_ledger_prepare_change_set,
    tool_ledger_prepare_balance_reconciliation,
    tool_ledger_prepare_balance_update,
]

ANALYTICS_TOOLS = [
    tool_account_balance,
    tool_ledger_calculate_balance_adjustment,
    tool_find_transactions,
    tool_query_template,
    tool_query,
    tool_query_report,
    tool_fetch_price,
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
