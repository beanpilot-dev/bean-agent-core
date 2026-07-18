"""Account-close preparation policy and plan construction."""

from datetime import date
from typing import Any

from ...account_lifecycle import account_close_state_payload, inspect_account_close
from ...beancount import Beancount
from ...ledger_paths import sidecar_target_file
from ...reconciliation import is_valid_account_name
from ...types import InvariantViolation, LedgerConfig
from ..facts import capture_account_close_state_fact
from ..planners import MutationPlanner
from .contracts import PreparedMutation


class AccountClosePreparationHandler:
    """Prepare one exact, zero-inventory account close for explicit approval."""

    handler_key = "close_account"

    def build(
        self,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: Any,
    ) -> PreparedMutation | InvariantViolation:
        account_name = str(kwargs.get("account_name") or "")
        close_date_text = str(kwargs.get("close_date") or "")
        commit_message = str(kwargs.get("commit_message") or "")

        if not is_valid_account_name(account_name):
            return _invalid(
                "ACCOUNT_NAME_FORMAT",
                account_name,
                "Provide the exact full Beancount account name returned by ledger_find_accounts.",
            )
        parsed_close_date = _parse_iso_date(close_date_text)
        if parsed_close_date is None:
            return _invalid(
                "ACCOUNT_CLOSE_DATE_FORMAT",
                close_date_text,
                "Provide the close date as an ISO calendar date in YYYY-MM-DD format.",
            )

        parsed = Beancount.parsed_ledger(workspace, ledger_config)
        if parsed.errors:
            return _invalid(
                "LEDGER_PARSE_ERROR",
                account_name,
                (
                    "Refresh the ledger and resolve validation errors before preparing "
                    "an account close."
                ),
            )
        state = inspect_account_close(
            workspace, account_name, parsed_close_date, ledger_config
        )
        if state is None:
            return _invalid(
                "ACCOUNT_NOT_FOUND",
                account_name,
                "Use ledger_find_accounts with status=all and pass an exact existing account name.",
            )
        if state.open_date is None:
            return _invalid(
                "ACCOUNT_NOT_OPEN",
                account_name,
                "The account has no parsed open directive. Open it before preparing a close.",
            )
        if state.status == "closed":
            return _invalid(
                "ACCOUNT_ALREADY_CLOSED",
                account_name,
                "The exact account is already closed; do not prepare a second close directive.",
            )
        if parsed_close_date.isoformat() < state.open_date:
            return _invalid(
                "ACCOUNT_CLOSE_BEFORE_OPEN",
                {
                    "account": account_name,
                    "open_date": state.open_date,
                    "close_date": close_date_text,
                },
                "Choose a close date on or after the account's open date.",
            )
        if state.future_postings:
            return _invalid(
                "ACCOUNT_HAS_FUTURE_POSTINGS",
                account_close_state_payload(state),
                (
                    "Move or remove postings after the requested close date, then "
                    "prepare the close again."
                ),
            )
        if state.inventory:
            return _invalid(
                "ACCOUNT_NONZERO_INVENTORY",
                account_close_state_payload(state),
                "Transfer or reconcile every non-zero commodity before closing the account.",
            )

        directive = f"{close_date_text} close {account_name}"
        plan = MutationPlanner.account_close(directive, commit_message).with_semantic_facts(
            (
                capture_account_close_state_fact(
                    workspace, account_name, close_date_text, ledger_config
                ),
            )
        )
        target = sidecar_target_file(ledger_config)
        facts = account_close_state_payload(state)
        return PreparedMutation(
            handler_key=self.handler_key,
            action_type="close_account",
            plan=plan,
            preview_fields={
                "account": account_name,
                "open_date": state.open_date,
                "close_date": close_date_text,
                "last_posting_date": state.last_posting_date,
                "last_posting_date_at_or_before_close": state.last_posting_date_at_or_before_close,
                "inventory_at_close": list(state.inventory),
                "inventory_commodities": list(state.inventory_commodities),
                "balance_status": "zero",
                "directive": directive,
                "target_file": target,
                "commit_message": commit_message,
                "validation_status": "account_lifecycle_and_balance_validated",
            },
            execution_spec={
                "account_name": account_name,
                "close_date": close_date_text,
                "commit_message": commit_message,
            },
            display_fields={
                "kind": "account_close_preview",
                "title": "Close account",
                "summary": "Append one native close directive to the agent sidecar.",
                "directive": directive,
                "account": account_name,
                "open_date": state.open_date,
                "close_date": close_date_text,
                "last_posting_date": state.last_posting_date,
                "last_posting_date_at_or_before_close": state.last_posting_date_at_or_before_close,
                "inventory_at_close": list(state.inventory),
                "inventory_commodities": list(state.inventory_commodities),
                "balance_status": "zero",
                "target_file": target,
                "validation_status": "account_lifecycle_and_balance_validated",
                "diff": f"--- /dev/null\n+++ {target}\n@@ -0,0 +1 @@\n+{directive}\n",
            },
            validation_fields={
                "account": account_name,
                "open_date": state.open_date,
                "close_date": close_date_text,
                "last_posting_date": state.last_posting_date,
                "balance_status": "zero",
                "target_file": target,
                "validation_status": "account_lifecycle_and_balance_validated",
                "state_fact": facts,
            },
            message=(
                "Account close passed deterministic lifecycle, posting, inventory, and "
                "isolated bean-check validation. Request explicit approval."
            ),
            preview_target_field="target_file",
        )


def _parse_iso_date(value: str) -> date | None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _invalid(invariant: str, provided: object, remediation: str) -> InvariantViolation:
    return InvariantViolation(
        invariant=invariant,
        severity="HARD",
        provided=provided,
        remediation=remediation,
    )
