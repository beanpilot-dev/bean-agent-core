"""Ledger read capabilities: preflight, account queries, transaction search."""

import json
import logging
import os
import re

from . import _beancount as bc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_sidecar_include(workspace: str) -> bool:
    """Return True if data/main.beancount contains include "agent_inc/main.beancount"."""
    main = os.path.join(workspace, "data", "main.beancount")
    try:
        with open(main) as f:
            return 'include "agent_inc/main.beancount"' in f.read()
    except OSError:
        return False


def _ensure_agent_sidecar(workspace: str) -> str:
    """Ensure the agent_inc/ sidecar structure exists for the current month.

    Creates if absent:
        data/agent_inc/main.beancount      — aggregator with include directives
        data/agent_inc/YYYY-MM.beancount   — current month chunk

    Returns the relative path of the current chunk: data/agent_inc/YYYY-MM.beancount.
    """
    from datetime import date
    today = date.today()
    chunk_name = f"{today.year}-{today.month:02d}.beancount"
    agent_dir = os.path.join(workspace, "data", "agent_inc")
    os.makedirs(agent_dir, exist_ok=True)

    chunk_path = os.path.join(agent_dir, chunk_name)
    if not os.path.exists(chunk_path):
        with open(chunk_path, "w") as f:
            f.write(f"; Agent-generated transactions — {today.year}-{today.month:02d}\n")

    agg_path = os.path.join(agent_dir, "main.beancount")
    include_line = f'include "{chunk_name}"\n'
    if os.path.exists(agg_path):
        with open(agg_path) as f:
            existing = f.read()
        if chunk_name not in existing:
            with open(agg_path, "a") as f:
                f.write(include_line)
    else:
        with open(agg_path, "w") as f:
            f.write("; Agent sidecar — auto-managed, do not edit manually\n")
            f.write(include_line)

    return f"data/agent_inc/{chunk_name}"


def get_agent_target_file(workspace: str) -> str:
    """Return the current agent write target, creating it if needed.

    Always returns data/agent_inc/YYYY-MM.beancount for the current month.
    """
    return _ensure_agent_sidecar(workspace)


def get_accounts(workspace: str) -> list[str]:
    """Return all accounts currently defined in the ledger."""
    rows, error = bc.run_bql_rows(
        workspace, "SELECT DISTINCT account ORDER BY account"
    )
    if error:
        return []
    return [r["account"] for r in rows]


def get_recent_transactions(workspace: str, target_file: str) -> str:
    """Return raw text of the last 5 transactions from the target file."""
    path = os.path.join(workspace, target_file)
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return ""
    txn_lines = [i for i, line in enumerate(lines) if re.match(r"^\d{4}-\d{2}-\d{2} ", line)]
    start = txn_lines[-5] if len(txn_lines) >= 5 else (txn_lines[0] if txn_lines else 0)
    return "".join(lines[start:]).strip()


# ---------------------------------------------------------------------------
# Public capabilities
# ---------------------------------------------------------------------------

def preflight_report(workspace: str) -> str:
    """Run preflight and return a structured text report for the agent.

    Returns STATUS, TARGET file, ACCOUNTS list, and RECENT transactions.
    Returns STATUS: SETUP_REQUIRED if the sidecar include is missing from main.beancount.
    """
    if not _check_sidecar_include(workspace):
        return (
            "STATUS: SETUP_REQUIRED\n"
            "ACTION: Add the following line to your data/main.beancount file "
            "to enable the agent sidecar:\n\n"
            '    include "agent_inc/main.beancount"\n\n'
            "Then retry the request."
        )

    target = _ensure_agent_sidecar(workspace)
    is_clean, check_output = bc.bean_check(workspace)
    account_list = get_accounts(workspace)
    recent = get_recent_transactions(workspace, target)

    lines = [
        f"STATUS: {'CLEAN' if is_clean else 'ERROR'}",
        f"TARGET: {target}",
    ]
    if not is_clean:
        lines += ["ERRORS:", check_output.strip()]
    lines += ["ACCOUNTS:"] + account_list + ["", "RECENT:", recent]
    return "\n".join(lines)


def get_balance(workspace: str, account: str, as_of_date: str | None = None) -> str:
    """Query the current balance of a specific account. Returns a JSON string."""
    date_clause = f"AND date < {as_of_date}" if as_of_date else ""
    bql = f'SELECT sum(position) AS balance WHERE account ~ "^{account}$" {date_clause}'

    rows, error = bc.run_bql_rows(workspace, bql)
    if error is not None:
        return json.dumps({
            "status": "ERROR",
            "error": error,
        })

    balance_raw = rows[0].get("balance", "").strip() if rows else ""
    return json.dumps({
        "status": "SUCCESS",
        "result": {
            "account": account,
            "as_of": as_of_date or "latest",
            "balance": balance_raw if balance_raw else "0",
        },
    })


def find_transactions(
    workspace: str,
    account: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    narration_contains: str | None = None,
    limit: int = 20,
) -> str:
    """Search for transactions matching one or more filters. Returns a JSON string."""
    filters = []
    if account:
        filters.append(f'account ~ "{account}"')
    if date_from:
        filters.append(f"date >= {date_from}")
    if date_to:
        filters.append(f"date <= {date_to}")
    if narration_contains:
        filters.append(f'narration ~ "{re.escape(narration_contains)}"')

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    bql = (
        f"SELECT date, flag, payee, narration, account, position "
        f"{where_clause} ORDER BY date DESC LIMIT {limit}"
    )

    rows, error = bc.run_bql_rows(workspace, bql)
    if error is not None:
        return json.dumps({
            "status": "ERROR",
            "error": error,
        })

    return json.dumps({
        "status": "SUCCESS",
        "result": {
            "count": len(rows),
            "filters_applied": {
                "account": account,
                "date_from": date_from,
                "date_to": date_to,
                "narration_contains": narration_contains,
                "limit": limit,
            },
            "rows": rows,
        },
    })


def query_bql(workspace: str, bql: str) -> str:
    """Execute an arbitrary BQL query and return JSON results."""
    rows, error = bc.run_bql_rows(workspace, bql)
    if error is not None:
        return json.dumps({"status": "ERROR", "error": error})
    return json.dumps({"status": "SUCCESS", "result": {"count": len(rows), "rows": rows}})


def find_transaction_block(
    workspace: str, date: str, narration: str
) -> list[tuple[str, str, str]]:
    """Search all beancount files for a transaction matching date + narration.

    Uses bean-query to validate the transaction exists across ALL included
    files, then walks data/ for .beancount files to extract raw blocks.

    Returns a list of (rel_path, file_content, raw_block) tuples.
    Empty list = not found; more than one = ambiguous.
    """
    escaped_narration = narration.replace('"', '\\"')
    bql = (
        f'SELECT DISTINCT date, narration '
        f'WHERE date = {date} AND narration ~ "{escaped_narration}"'
    )
    rows, error = bc.run_bql_rows(workspace, bql)
    if error or not rows:
        return []

    header_re = re.compile(
        rf"^{re.escape(date)}\s+[*!].*?{re.escape(narration)}",
        re.MULTILINE,
    )

    files_to_search: list[tuple[str, str]] = []
    data_dir = os.path.join(workspace, "data")

    try:
        for dirpath, _dirnames, filenames in os.walk(data_dir):
            for fname in sorted(filenames):
                if not fname.endswith(".beancount"):
                    continue
                files_to_search.append(
                    (os.path.relpath(os.path.join(dirpath, fname), workspace),
                     os.path.join(dirpath, fname))
                )
    except OSError:
        pass

    results = []
    for rel_path, abs_path in files_to_search:
        try:
            with open(abs_path) as f:
                content = f.read()
        except OSError:
            continue
        for m in header_re.finditer(content):
            block_start = m.start()
            rest = content[block_start:]
            end_match = re.search(r"\n[ \t]*\n", rest)
            raw_block = rest[: end_match.start()].rstrip() if end_match else rest.rstrip()
            results.append((rel_path, content, raw_block))

    return results
