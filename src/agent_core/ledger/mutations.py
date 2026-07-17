"""Ledger write capabilities: commit transaction, update transaction, open account."""

import json
import logging
import os
import re

from . import _beancount as bc
from . import workspace as ws
from .state import find_transaction_block, get_accounts, get_agent_target_file

logger = logging.getLogger(__name__)

# Beancount account name pattern (for open_account validation)
_ACCOUNT_NAME_RE = re.compile(
    r"^(Assets|Liabilities|Equity|Income|Expenses)(:[A-Z][A-Za-z0-9\-]+)+$"
)

# Matches an account name at the start of a posting line
_POSTING_ACCOUNT_RE = re.compile(
    r"^\s+(Assets|Liabilities|Equity|Income|Expenses)(?::[A-Za-z][A-Za-z0-9\-]+)+",
    re.MULTILINE,
)

# Matches amount values in a posting (for VALUE_CHANGED detection)
_AMOUNT_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*\s+[A-Z][A-Z0-9\-]+")


def _extract_accounts(transaction_text: str) -> list[str]:
    """Return the set of account names referenced in a transaction block."""
    return list({m.group(0).strip() for m in _POSTING_ACCOUNT_RE.finditer(transaction_text)})


def _validate_accounts(
    workspace: str,
    transaction_text: str,
    whitelist: list[str] | None = None,
) -> dict | None:
    """Check all posting accounts exist in the ledger whitelist.

    Also enforces the per-conversation account whitelist when one is active.
    Returns an INVARIANT_VIOLATION dict if unknown or out-of-scope accounts are found, else None.
    """
    used = _extract_accounts(transaction_text)
    valid = set(get_accounts(workspace))
    unknown = [a for a in used if a not in valid]
    if unknown:
        return {
            "status": "INVARIANT_VIOLATION",
            "invariant": "ACCOUNT_WHITELIST",
            "severity": "HARD",
            "provided": unknown,
            "valid_accounts": sorted(valid),
            "remediation": (
                "Unknown accounts detected. "
                "Use ledger_open_account to create them first, "
                "then call this tool again."
            ),
        }

    # Per-conversation scope restriction (optional)
    if whitelist:
        out_of_scope = [a for a in used if not any(a.startswith(w) for w in whitelist)]
        if out_of_scope:
            return {
                "status": "INVARIANT_VIOLATION",
                "invariant": "CONVERSATION_SCOPE",
                "severity": "HARD",
                "provided": out_of_scope,
                "allowed_prefixes": whitelist,
                "remediation": (
                    "These accounts are outside the current conversation's scope. "
                    "Use accounts within the allowed prefixes, or start a new "
                    "conversation without a whitelist restriction."
                ),
            }

    return None


def _detect_value_change(old_text: str, new_text: str) -> dict | None:
    """Return an ADVISORY dict if amounts or accounts differ between old and new text."""
    old_amounts = set(_AMOUNT_RE.findall(old_text))
    new_amounts = set(_AMOUNT_RE.findall(new_text))
    old_accounts = {m.group(0).strip() for m in _POSTING_ACCOUNT_RE.finditer(old_text)}
    new_accounts = {m.group(0).strip() for m in _POSTING_ACCOUNT_RE.finditer(new_text)}

    changes: dict = {}
    if old_amounts != new_amounts:
        changes["amounts"] = {
            "removed": sorted(old_amounts - new_amounts),
            "added": sorted(new_amounts - old_amounts),
        }
    if old_accounts != new_accounts:
        changes["accounts"] = {
            "removed": sorted(old_accounts - new_accounts),
            "added": sorted(new_accounts - old_accounts),
        }
    if not changes:
        return None

    return {
        "severity": "ADVISORY",
        "warning": "VALUE_CHANGED",
        "changes": changes,
        "note": (
            "Amount or account changes shift running balances. "
            "If balance assertions exist after this transaction, bean-check may fail. "
            "The application will auto-revert on failure — no data will be lost."
        ),
    }


def _git_error_response(git: dict) -> str:
    return json.dumps({
        "status": "DEPENDENCY_UNAVAILABLE",
        "error": f"Written but git commit failed: {git['error']}",
        "retryable": False,
        "note": "The file was modified but not committed. Manual git intervention required.",
    })


# ---------------------------------------------------------------------------
# Public capabilities
# ---------------------------------------------------------------------------

def commit_transaction(
    workspace: str,
    transaction_text: str,
    commit_message: str,
    github_token: str | None = None,
    confirmed: bool = False,
    whitelist: list[str] | None = None,
) -> str:
    """Record a new transaction atomically.

    confirmed=False: validates accounts, returns PREVIEW. Nothing written.
    confirmed=True:  appends, bean-checks (auto-reverts on failure), commits, pushes.

    Returns a JSON string.
    """
    target = get_agent_target_file(workspace)
    if not target:
        return json.dumps({
            "status": "DEPENDENCY_UNAVAILABLE",
            "error": "Could not create agent_inc target file.",
            "retryable": False,
        })

    violation = _validate_accounts(workspace, transaction_text, whitelist)
    if violation:
        return json.dumps(violation)

    if not confirmed:
        return json.dumps({
            "status": "PREVIEW",
            "outcome": f"Transaction will be appended to {target}",
            "transaction": transaction_text,
            "accounts_validated": sorted(_extract_accounts(transaction_text)),
            "target_file": target,
            "commit_message": commit_message,
            "message": (
                "All accounts validated. "
                "Show this preview to the user and call ledger_commit "
                "with confirmed=True after explicit approval."
            ),
            "next_capabilities": ["ledger_commit (confirmed=True)"],
        })

    # confirmed=True — atomic write + validate + commit
    target_path = os.path.join(workspace, target)
    backup_path = target_path + ".bak"

    with open(target_path) as f:
        original = f.read()
    with open(backup_path, "w") as f:
        f.write(original)

    with open(target_path, "a") as f:
        f.write(f"\n{transaction_text}\n")

    is_clean, check_output = bc.bean_check(workspace)
    if not is_clean:
        with open(target_path, "w") as f:
            f.write(original)
        os.remove(backup_path)
        return json.dumps({
            "status": "VALIDATION_FAILED",
            "invariant": "BEANCOUNT_SYNTAX",
            "severity": "HARD",
            "error": check_output.strip(),
            "reverted": True,
            "remediation": "Fix the transaction syntax and call ledger_commit again.",
        })

    os.remove(backup_path)
    bc.bean_format(workspace, target_path)

    git = ws.commit_and_push(workspace, commit_message, token=github_token)
    if not git["ok"]:
        return _git_error_response(git)

    return json.dumps({
        "status": "SUCCESS",
        "outcome": "Transaction recorded, validated, and committed",
        "result": {
            "target_file": target,
            "commit_message": commit_message,
            "push": git["push"],
            "transaction": transaction_text,
        },
        "side_effects": [
            f"Appended to {target}",
            f"git commit: {commit_message}",
            git["push"],
        ],
        "next_capabilities": ["ledger_find_transactions", "ledger_query"],
    })


def update_transaction(
    workspace: str,
    date: str,
    narration: str,
    new_transaction_text: str,
    commit_message: str,
    github_token: str | None = None,
    confirmed: bool = False,
    whitelist: list[str] | None = None,
) -> str:
    """Find a transaction by date + narration and replace it atomically.

    confirmed=False: finds transaction, validates, returns PREVIEW (with ADVISORY if values change).
    confirmed=True:  replaces, bean-checks (auto-reverts on failure), commits, pushes.

    Returns a JSON string.
    """
    matches = find_transaction_block(workspace, date, narration)

    if not matches:
        return json.dumps({
            "status": "INVARIANT_VIOLATION",
            "invariant": "TRANSACTION_NOT_FOUND",
            "severity": "HARD",
            "provided": {"date": date, "narration": narration},
            "remediation": (
                "No transaction found matching this date and narration. "
                "Use ledger_find_transactions to locate the exact entry, "
                "then retry with the correct date and narration substring."
            ),
        })

    if len(matches) > 1:
        return json.dumps({
            "status": "INVARIANT_VIOLATION",
            "invariant": "AMBIGUOUS_MATCH",
            "severity": "HARD",
            "provided": {"date": date, "narration": narration},
            "matches_found": [
                {"file": rel, "block": block} for rel, _, block in matches
            ],
            "remediation": (
                "Multiple transactions match this date and narration. "
                "Provide a more specific narration substring to identify the correct one."
            ),
        })

    rel_path, file_content, old_block = matches[0]

    violation = _validate_accounts(workspace, new_transaction_text, whitelist)
    if violation:
        return json.dumps(violation)

    advisory = _detect_value_change(old_block, new_transaction_text)

    if not confirmed:
        return json.dumps({
            "status": "PREVIEW",
            "outcome": f"Will replace transaction in {rel_path}",
            "found_block": old_block,
            "replacement": new_transaction_text.strip(),
            "file": rel_path,
            "commit_message": commit_message,
            "advisory": advisory,
            "message": (
                "Transaction located. "
                + (f"Advisory: {advisory['note']} " if advisory else "")
                + "Call ledger_update_transaction with confirmed=True after user approval."
            ),
            "next_capabilities": ["ledger_update_transaction (confirmed=True)"],
        })

    # confirmed=True — replace + validate + commit
    file_path = os.path.join(workspace, rel_path)
    backup_path = file_path + ".bak"

    with open(file_path) as f:
        original = f.read()
    with open(backup_path, "w") as f:
        f.write(original)

    new_content = original.replace(old_block, new_transaction_text.strip(), 1)
    with open(file_path, "w") as f:
        f.write(new_content)

    is_clean, check_output = bc.bean_check(workspace)
    if not is_clean:
        with open(file_path, "w") as f:
            f.write(original)
        os.remove(backup_path)
        return json.dumps({
            "status": "VALIDATION_FAILED",
            "invariant": "BEANCOUNT_SYNTAX",
            "severity": "HARD",
            "error": check_output.strip(),
            "reverted": True,
            "advisory": advisory,
            "remediation": (
                "bean-check failed after replacement — file was auto-reverted. "
                + (
                    "A balance assertion is likely broken by the value change. "
                    "Adjust the balance assertion or restore the original amount."
                    if advisory
                    else "Fix the transaction syntax and try again."
                )
            ),
        })

    os.remove(backup_path)
    bc.bean_format(workspace, file_path)

    git = ws.commit_and_push(workspace, commit_message, token=github_token)
    if not git["ok"]:
        return _git_error_response(git)

    return json.dumps({
        "status": "SUCCESS",
        "outcome": "Transaction updated, validated, and committed",
        "result": {
            "file": rel_path,
            "old_block": old_block,
            "new_block": new_transaction_text.strip(),
            "commit_message": commit_message,
            "push": git["push"],
        },
        "side_effects": [
            f"Transaction replaced in {rel_path}",
            f"git commit: {commit_message}",
            git["push"],
        ],
        "next_capabilities": ["ledger_find_transactions", "ledger_query"],
    })


def open_account(
    workspace: str,
    account_name: str,
    currency: str | None,
    open_date: str,
    display_name: str | None = None,
    confirmed: bool = False,
    github_token: str | None = None,
) -> str:
    """Add a new account open directive to main.beancount.

    confirmed=False: validates name/uniqueness, returns PREVIEW.
    confirmed=True:  writes, bean-checks (auto-reverts on failure), commits, pushes.

    Returns a JSON string.
    """
    # Invariant: account name format (HARD)
    if not _ACCOUNT_NAME_RE.match(account_name):
        return json.dumps({
            "status": "INVARIANT_VIOLATION",
            "invariant": "ACCOUNT_NAME_FORMAT",
            "severity": "HARD",
            "provided": account_name,
            "remediation": (
                "Account names must follow Beancount format: Type:Component "
                "(e.g. Assets:Liquid:Bank:NewAccount). "
                "First component must be Assets, Liabilities, Equity, Income, or Expenses. "
                "Each component starts with an uppercase letter."
            ),
        })

    # Invariant: account must not already exist (HARD)
    existing = get_accounts(workspace)
    if account_name in existing:
        return json.dumps({
            "status": "INVARIANT_VIOLATION",
            "invariant": "ACCOUNT_ALREADY_EXISTS",
            "severity": "HARD",
            "provided": account_name,
            "remediation": (
                f"Account '{account_name}' already exists. Use it directly in transactions."
            ),
        })

    # Build the directive text
    currency_part = f"  {currency}" if currency else ""
    directive_lines = [f"{open_date} open {account_name}{currency_part}"]
    if display_name:
        directive_lines.append(f'  name: "{display_name}"')
    directive_text = "\n".join(directive_lines)

    if not confirmed:
        return json.dumps({
            "status": "PREVIEW",
            "outcome": "Will add open directive to data/agent_inc/main.beancount",
            "directive": directive_text,
            "account": account_name,
            "currency": currency,
            "open_date": open_date,
            "message": (
                "Account directive validated. "
                "Call ledger_open_account with confirmed=True to apply."
            ),
        })

    # confirmed=True — write + validate + commit
    from .state import _ensure_agent_sidecar
    _ensure_agent_sidecar(workspace)
    main_path = os.path.join(workspace, "data", "agent_inc", "main.beancount")
    try:
        with open(main_path) as f:
            original = f.read()
    except OSError as e:
        return json.dumps({
            "status": "DEPENDENCY_UNAVAILABLE",
            "error": f"Cannot read main.beancount: {e}",
            "retryable": False,
        })

    # Insert after last open directive of the same account type
    account_type = account_name.split(":")[0]
    lines = original.splitlines()
    insert_after = -1
    for i, line in enumerate(lines):
        if re.match(rf"^\d{{4}}-\d{{2}}-\d{{2}} open {account_type}", line):
            insert_after = i
    if insert_after == -1:
        for i, line in enumerate(lines):
            if re.match(r"^\d{4}-\d{2}-\d{2} open ", line):
                insert_after = i

    if insert_after >= 0:
        for j, dl in enumerate(directive_lines):
            lines.insert(insert_after + 1 + j, dl)
        new_content = "\n".join(lines) + "\n"
    else:
        new_content = original.rstrip("\n") + "\n\n" + directive_text + "\n"

    try:
        with open(main_path, "w") as f:
            f.write(new_content)
    except OSError as e:
        return json.dumps({
            "status": "DEPENDENCY_UNAVAILABLE",
            "error": f"Cannot write main.beancount: {e}",
            "retryable": False,
        })

    is_clean, check_output = bc.bean_check(workspace)
    if not is_clean:
        with open(main_path, "w") as f:
            f.write(original)
        return json.dumps({
            "status": "VALIDATION_FAILED",
            "invariant": "BEANCOUNT_SYNTAX",
            "severity": "HARD",
            "error": check_output.strip(),
            "reverted": True,
            "remediation": "Fix the account directive and try again.",
        })

    bc.bean_format(workspace, main_path)
    git = ws.commit_and_push(
        workspace,
        message=f"chore(accounts): open {account_name}",
        token=github_token,
    )
    if not git["ok"]:
        return _git_error_response(git)

    return json.dumps({
        "status": "SUCCESS",
        "outcome": f"Account '{account_name}' opened and committed",
        "result": {
            "account": account_name,
            "currency": currency,
            "open_date": open_date,
            "file": "data/agent_inc/main.beancount",
            "push": git["push"],
        },
        "side_effects": [
            "data/agent_inc/main.beancount updated",
            f"git commit: chore(accounts): open {account_name}",
            git["push"],
        ],
        "next_capabilities": ["ledger_commit_transaction"],
    })


def bulk_commit_transactions(
    workspace: str,
    transactions_text: str,
    commit_message: str,
    github_token: str | None = None,
    confirmed: bool = False,
    transactions_file: str | None = None,
    whitelist: list[str] | None = None,
) -> str:
    """Append multiple transactions at once.

    confirmed=False: validates all accounts, returns PREVIEW with count + first 5 transactions.
    confirmed=True:  appends all, bean-checks (auto-reverts on failure), commits, pushes.

    Supply the transaction content via exactly one of:
      transactions_text  — the raw beancount text (for small batches)
      transactions_file  — path to a /tmp staging file from ledger_run_python(stage=True)

    Returns a JSON string.
    """
    # Resolve content: file takes priority when both supplied; require one of the two
    if transactions_file:
        try:
            with open(transactions_file, encoding="utf-8") as f:
                transactions_text = f.read()
        except OSError as e:
            return json.dumps({
                "status": "DEPENDENCY_UNAVAILABLE",
                "error": f"Cannot read staging file {transactions_file}: {e}",
                "retryable": False,
            })
    elif not transactions_text:
        return json.dumps({
            "status": "INVARIANT_VIOLATION",
            "invariant": "MISSING_INPUT",
            "severity": "HARD",
            "remediation": "Provide either transactions_text or transactions_file.",
        })

    target = get_agent_target_file(workspace)
    if not target:
        return json.dumps({
            "status": "DEPENDENCY_UNAVAILABLE",
            "error": "Could not create agent_inc target file.",
            "retryable": False,
        })

    violation = _validate_accounts(workspace, transactions_text, whitelist)
    if violation:
        return json.dumps(violation)

    # Count transactions (lines starting with a date + flag)
    txn_lines = [
        line for line in transactions_text.splitlines()
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+[*!]", line)
    ]
    txn_count = len(txn_lines)

    # Preview: first 5 transaction header lines as a sample
    sample = "\n".join(txn_lines[:5])
    if txn_count > 5:
        sample += f"\n... ({txn_count - 5} more)"

    if not confirmed:
        return json.dumps({
            "status": "PREVIEW",
            "outcome": f"{txn_count} transactions will be appended to {target}",
            "transaction_count": txn_count,
            "sample": sample,
            "target_file": target,
            "commit_message": commit_message,
            "message": (
                f"All accounts validated. {txn_count} transactions ready. "
                "Show this preview to the user and call ledger_bulk_commit "
                "with confirmed=True after explicit approval."
            ),
            "next_capabilities": ["ledger_bulk_commit (confirmed=True)"],
        })

    target_path = os.path.join(workspace, target)
    backup_path = target_path + ".bak"

    with open(target_path) as f:
        original = f.read()
    with open(backup_path, "w") as f:
        f.write(original)

    with open(target_path, "a") as f:
        f.write(f"\n{transactions_text.strip()}\n")

    is_clean, check_output = bc.bean_check(workspace)
    if not is_clean:
        with open(target_path, "w") as f:
            f.write(original)
        os.remove(backup_path)
        return json.dumps({
            "status": "VALIDATION_FAILED",
            "invariant": "BEANCOUNT_SYNTAX",
            "severity": "HARD",
            "error": check_output.strip(),
            "reverted": True,
            "remediation": (
                "bean-check failed — all transactions were auto-reverted. "
                "Fix the syntax errors in the batch and retry."
            ),
        })

    os.remove(backup_path)
    bc.bean_format(workspace, target_path)

    git = ws.commit_and_push(workspace, commit_message, token=github_token)
    if not git["ok"]:
        return _git_error_response(git)

    # Clean up staging file if one was used
    if transactions_file and os.path.exists(transactions_file):
        try:
            os.remove(transactions_file)
        except OSError:
            pass  # non-fatal; /tmp will be cleaned by the OS eventually

    return json.dumps({
        "status": "SUCCESS",
        "outcome": f"{txn_count} transactions recorded, validated, and committed",
        "result": {
            "target_file": target,
            "transaction_count": txn_count,
            "commit_message": commit_message,
            "push": git["push"],
        },
        "side_effects": [
            f"Appended {txn_count} transactions to {target}",
            f"git commit: {commit_message}",
            git["push"],
        ],
        "next_capabilities": ["ledger_find_transactions", "ledger_query"],
    })
