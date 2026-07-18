"""Transaction-commit preparation policy and plan construction."""

import re
from typing import Any

from ...queries import LedgerQueryService
from ...types import InvariantViolation, LedgerConfig
from ..facts import capture_account_state_fact
from ..planners import MutationPlanner
from ..plans import MutationOperation
from .contracts import PreparedMutation

_POSTING_ACCOUNT_RE = re.compile(
    r"^\s+(Assets|Liabilities|Equity|Income|Expenses)(?::[A-Za-z][A-Za-z0-9\-]+)+",
    re.MULTILINE,
)


def extract_posting_accounts(transaction_text: str) -> list[str]:
    """Return the account read-set used by transaction mutation policy."""
    return sorted(
        {match.group(0).strip() for match in _POSTING_ACCOUNT_RE.finditer(transaction_text)}
    )


def validate_posting_accounts(
    workspace: str,
    transaction_text: str,
    whitelist: list[str] | None = None,
    ledger_config: LedgerConfig | None = None,
    known_accounts: set[str] | None = None,
) -> InvariantViolation | None:
    """Enforce ledger-account existence and conversation scope."""
    used = extract_posting_accounts(transaction_text)
    valid = known_accounts or set(LedgerQueryService.get_accounts(workspace, ledger_config))
    unknown = [account for account in used if account not in valid]
    if unknown:
        return InvariantViolation(
            invariant="ACCOUNT_WHITELIST",
            severity="HARD",
            provided=unknown,
            remediation="Unknown accounts detected. Use open_account to create them first.",
            detail={"valid_accounts": sorted(valid)},
        )
    if whitelist:
        out_of_scope = [
            account
            for account in used
            if not any(account.startswith(prefix) for prefix in whitelist)
        ]
        if out_of_scope:
            return InvariantViolation(
                invariant="CONVERSATION_SCOPE",
                severity="HARD",
                provided=out_of_scope,
                remediation=(
                    "These accounts are outside the current conversation scope. "
                    "Use accounts within the allowed prefixes."
                ),
                detail={"allowed_prefixes": whitelist},
            )
    return None


def transaction_operation(transaction_text: str) -> MutationOperation:
    """Build the canonical append operation used by standalone and composite writes."""
    return MutationOperation(kind="append", text=transaction_text)


def transaction_account_facts(
    workspace: str,
    accounts: list[str],
    ledger_config: LedgerConfig | None = None,
) -> tuple:
    """Capture the canonical account lifecycle read set for a transaction."""
    return tuple(
        capture_account_state_fact(workspace, account, ledger_config)
        for account in accounts
    )


class TransactionCommitPreparationHandler:
    handler_key = "commit_transaction"

    def build(
        self,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: Any,
    ) -> PreparedMutation | InvariantViolation:
        transaction_text = str(kwargs["transaction_text"])
        commit_message = str(kwargs["commit_message"])
        whitelist = kwargs.get("whitelist")
        if whitelist is not None and not isinstance(whitelist, list):
            whitelist = None
        violation = validate_posting_accounts(
            workspace, transaction_text, whitelist, ledger_config
        )
        if violation:
            return violation
        accounts = extract_posting_accounts(transaction_text)
        plan = MutationPlanner.commit(transaction_text, commit_message).with_semantic_facts(
            transaction_account_facts(workspace, accounts, ledger_config)
        )
        return PreparedMutation(
            handler_key=self.handler_key,
            action_type="commit_transaction",
            plan=plan,
            preview_fields={
                "transaction": transaction_text,
                "accounts_validated": accounts,
                "commit_message": commit_message,
            },
            execution_spec={
                "transaction_text": transaction_text,
                "commit_message": commit_message,
            },
            display_fields={
                "kind": "transaction_preview",
                "summary": "Record a transaction",
                "diff": transaction_text,
            },
            validation_fields={"accounts": accounts},
            message=(
                "All accounts and dry-run validation passed. Show this preview "
                "to the user and request explicit approval."
            ),
            preview_target_field="target_file",
        )
