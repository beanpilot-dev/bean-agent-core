"""Ordered composite-change policy and immutable plan construction."""

import re
from typing import Any

from ...beancount import _cfg
from ...ledger_paths import sidecar_target_file
from ...queries import LedgerQueryService
from ...types import InvariantViolation, LedgerConfig
from ..facts import capture_account_state_fact
from ..planners import MutationPlanner
from ..plans import MutationOperation
from .contracts import PreparedMutation
from .transaction_commit import extract_posting_accounts

_ACCOUNT_NAME_RE = re.compile(
    r"^(Assets|Liabilities|Equity|Income|Expenses)(:[A-Z][A-Za-z0-9\-]+)+$"
)


def _operation_type(operation: dict[str, object]) -> str:
    value = operation.get("type") or operation.get("operation")
    return str(value or "")


def _operation_error(
    *,
    operation_index: int,
    invariant: str,
    provided: object,
    remediation: str,
    detail: dict[str, object] | None = None,
) -> InvariantViolation:
    return InvariantViolation(
        invariant=invariant,
        severity="HARD",
        provided=provided,
        remediation=remediation,
        detail={"operation_index": operation_index, **(detail or {})},
    )


def _open_directive(operation: dict[str, object], account_name: str) -> str:
    directive = f"{operation.get('open_date') or ''} open {account_name}"
    currency = operation.get("currency")
    if isinstance(currency, str) and currency:
        directive += f"  {currency}"
    display_name = operation.get("display_name")
    if isinstance(display_name, str) and display_name:
        directive += f'\n  name: "{display_name}"'
    return directive


class ChangeSetPreparationHandler:
    """Validate ordered dependencies and describe one composite mutation."""

    handler_key = "change_set"

    def build(
        self,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: Any,
    ) -> PreparedMutation | InvariantViolation:
        operations = kwargs.get("operations")
        commit_message = str(kwargs.get("commit_message") or "")
        whitelist = kwargs.get("whitelist")
        if whitelist is not None and not isinstance(whitelist, list):
            whitelist = None
        if not isinstance(operations, list) or not operations:
            return InvariantViolation(
                invariant="MISSING_OPERATIONS",
                severity="HARD",
                provided=operations,
                remediation="Provide at least one change-set operation.",
            )

        known_accounts = set(LedgerQueryService.get_accounts(workspace, ledger_config))
        plan_operations: list[MutationOperation] = []
        display_items: list[dict[str, object]] = []
        affected_accounts: set[str] = set()
        transaction_count = 0
        config = _cfg(ledger_config)
        transaction_target = sidecar_target_file(config)

        for index, operation in enumerate(operations):
            if not isinstance(operation, dict):
                return _operation_error(
                    operation_index=index,
                    invariant="UNSUPPORTED_CHANGE_SET_OPERATION",
                    provided="",
                    remediation=(
                        "Only open_account and commit_transaction operations "
                        "are supported in change sets."
                    ),
                )
            operation_type = _operation_type(operation)
            if operation_type == "open_account":
                account_name = str(operation.get("account_name") or "")
                if not _ACCOUNT_NAME_RE.match(account_name):
                    return _operation_error(
                        operation_index=index,
                        invariant="ACCOUNT_NAME_FORMAT",
                        provided=account_name,
                        remediation=(
                            "Account names must follow Beancount format: "
                            "Type:Component (e.g. Assets:Liquid:Bank:NewAccount)."
                        ),
                    )
                if account_name in known_accounts:
                    return _operation_error(
                        operation_index=index,
                        invariant="ACCOUNT_ALREADY_EXISTS",
                        provided=account_name,
                        remediation=f"Account '{account_name}' already exists.",
                    )
                directive = _open_directive(operation, account_name)
                plan_operations.append(
                    MutationOperation(kind="open", account_name=account_name, text=directive)
                )
                display_items.append(
                    {
                        "operation_index": index,
                        "type": "open_account",
                        "summary": f"Open {account_name}",
                        "diff": directive,
                        "target_file": config.sidecar_main_path,
                    }
                )
                known_accounts.add(account_name)
                affected_accounts.add(account_name)
                continue

            if operation_type == "commit_transaction":
                transaction_text = str(operation.get("transaction_text") or "")
                accounts = extract_posting_accounts(transaction_text)
                unknown = [account for account in accounts if account not in known_accounts]
                if unknown:
                    return _operation_error(
                        operation_index=index,
                        invariant="ACCOUNT_WHITELIST",
                        provided=unknown,
                        remediation=(
                            "Unknown accounts detected. Use open_account to create them first."
                        ),
                        detail={"valid_accounts": sorted(known_accounts)},
                    )
                if whitelist:
                    out_of_scope = [
                        account
                        for account in accounts
                        if not any(account.startswith(prefix) for prefix in whitelist)
                    ]
                    if out_of_scope:
                        return _operation_error(
                            operation_index=index,
                            invariant="CONVERSATION_SCOPE",
                            provided=out_of_scope,
                            remediation="Use accounts within the allowed prefixes.",
                            detail={"allowed_prefixes": whitelist},
                        )
                plan_operations.append(MutationOperation(kind="append", text=transaction_text))
                display_items.append(
                    {
                        "operation_index": index,
                        "type": "commit_transaction",
                        "summary": "Record a transaction",
                        "diff": transaction_text,
                        "target_file": transaction_target,
                        "accounts": accounts,
                    }
                )
                affected_accounts.update(accounts)
                transaction_count += 1
                continue

            return _operation_error(
                operation_index=index,
                invariant="UNSUPPORTED_CHANGE_SET_OPERATION",
                provided=operation_type,
                remediation=(
                    "Only open_account and commit_transaction operations "
                    "are supported in change sets."
                ),
            )

        sorted_accounts = sorted(affected_accounts)
        plan = MutationPlanner.change_set(plan_operations, commit_message).with_semantic_facts(
            tuple(
                capture_account_state_fact(workspace, account, ledger_config)
                for account in sorted_accounts
            )
        )
        diff = "\n\n".join(str(item["diff"]) for item in display_items)
        target_files = list(dict.fromkeys(str(item["target_file"]) for item in display_items))
        return PreparedMutation(
            handler_key=self.handler_key,
            action_type="change_set",
            plan=plan,
            preview_fields={},
            execution_spec={
                "operations": operations,
                "commit_message": commit_message,
            },
            display_fields={
                "kind": "change_set_preview",
                "summary": f"Apply {len(operations)} related ledger changes",
                "diff": diff,
                "items": display_items,
            },
            validation_fields={
                "operation_count": len(operations),
                "transaction_count": transaction_count,
                "accounts": sorted_accounts,
                "target_files": target_files,
            },
            message="Change set passed dry-run validation. Request explicit approval.",
            embed_preview_in_display=False,
        )
