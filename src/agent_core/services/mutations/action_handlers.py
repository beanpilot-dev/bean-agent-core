"""Registered, action-specific mutation preparation handlers.

Handlers own the domain reads and construction for one mutation action.  The
preparation service deliberately only dispatches a handler, validates its plan,
seals it, and asks the handler to materialize the existing pending-action
contract.  Add a new action by registering another handler; do not add an
action-specific branch to shared preparation.
"""

import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from ..approvals.contracts import PendingActionService
from ..queries import LedgerQueryService
from ..types import InvariantViolation, LedgerConfig, PendingAction, Preview, ValidationSummary
from .facts import capture_account_state_fact
from .planners import MutationPlanner
from .plans import MutationPlan

_ACCOUNT_NAME_RE = re.compile(
    r"^(Assets|Liabilities|Equity|Income|Expenses)(:[A-Z][A-Za-z0-9\-]+)+$"
)
_POSTING_ACCOUNT_RE = re.compile(
    r"^\s+(Assets|Liabilities|Equity|Income|Expenses)(?::[A-Za-z][A-Za-z0-9\-]+)+",
    re.MULTILINE,
)


def extract_posting_accounts(transaction_text: str) -> list[str]:
    """Return the account read-set used by transaction-commit policy."""
    return sorted(
        {match.group(0).strip() for match in _POSTING_ACCOUNT_RE.finditer(transaction_text)}
    )


@dataclass(frozen=True)
class PreparedMutation:
    """Action-owned proposal facts before shared validation and sealing."""

    action_type: str
    plan: MutationPlan
    preview_fields: dict[str, object]
    execution_spec: dict[str, object]
    display_kind: str
    display_summary: str
    display_diff: str
    validation_fields: dict[str, object]
    message: str

    def preview(self, validation: ValidationSummary, target_file: str | None) -> Preview:
        fields = dict(self.preview_fields)
        fields["target_file"] = target_file
        fields["validation"] = asdict(validation)
        return Preview(
            proposal_id=f"prop_{uuid.uuid4().hex[:12]}",
            operation=self.action_type,
            preview=fields,
            message=self.message,
        )

    def pending_action(self, sealed_plan: dict[str, object], preview: Preview) -> PendingAction:
        """Keep action-specific pending payload ownership with its handler."""
        execution_spec = {"mutation_plan": sealed_plan, **self.execution_spec}
        validation = dict(self.validation_fields)
        validation["status"] = "validated"
        validation["dry_run"] = preview.preview.get("validation")
        return PendingActionService.create_pending_action(
            action_type=self.action_type,
            execution_spec=execution_spec,
            display={
                "kind": self.display_kind,
                "summary": self.display_summary,
                "diff": self.display_diff,
                "preview": preview.preview,
            },
            validation=validation,
        )


class MutationPreparationHandler(Protocol):
    """Build one action's plan and presentation facts from a read-only workspace."""

    action_type: str

    def build(
        self, workspace: str, ledger_config: LedgerConfig | None = None, **kwargs: Any
    ) -> PreparedMutation | InvariantViolation: ...


class TransactionCommitPreparationHandler:
    action_type = "commit_transaction"

    def build(
        self,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: Any,
    ) -> PreparedMutation | InvariantViolation:
        transaction_text = str(kwargs["transaction_text"])
        commit_message = str(kwargs["commit_message"])
        whitelist = kwargs.get("whitelist")
        accounts = extract_posting_accounts(transaction_text)
        valid = set(LedgerQueryService.get_accounts(workspace, ledger_config))
        unknown = [account for account in accounts if account not in valid]
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
                for account in accounts
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
        plan = MutationPlanner.commit(transaction_text, commit_message).with_semantic_facts(
            tuple(
                capture_account_state_fact(workspace, account, ledger_config)
                for account in accounts
            )
        )
        return PreparedMutation(
            action_type=self.action_type,
            plan=plan,
            preview_fields={
                "transaction": transaction_text,
                "accounts_validated": accounts,
                "commit_message": commit_message,
            },
            execution_spec={"transaction_text": transaction_text, "commit_message": commit_message},
            display_kind="transaction_preview",
            display_summary="Record a transaction",
            display_diff=transaction_text,
            validation_fields={"accounts": accounts},
            message=(
                "All accounts and dry-run validation passed. Show this preview "
                "to the user and request explicit approval."
            ),
        )


class AccountOpenPreparationHandler:
    action_type = "open_account"

    def build(
        self,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: Any,
    ) -> PreparedMutation | InvariantViolation:
        account_name = str(kwargs["account_name"])
        currency = kwargs.get("currency")
        open_date = str(kwargs["open_date"])
        display_name = kwargs.get("display_name")
        if not _ACCOUNT_NAME_RE.match(account_name):
            return InvariantViolation(
                invariant="ACCOUNT_NAME_FORMAT",
                severity="HARD",
                provided=account_name,
                remediation=(
                    "Account names must follow Beancount format: Type:Component "
                    "(e.g. Assets:Liquid:Bank:NewAccount)."
                ),
            )
        if account_name in LedgerQueryService.get_accounts(workspace, ledger_config):
            return InvariantViolation(
                invariant="ACCOUNT_ALREADY_EXISTS",
                severity="HARD",
                provided=account_name,
                remediation=f"Account '{account_name}' already exists.",
            )
        currency_part = f"  {currency}" if currency else ""
        directive_lines = [f"{open_date} open {account_name}{currency_part}"]
        if display_name:
            directive_lines.append(f'  name: "{display_name}"')
        directive_text = "\n".join(directive_lines)
        plan = MutationPlanner.open_account(account_name, directive_text).with_semantic_facts(
            (capture_account_state_fact(workspace, account_name, ledger_config),)
        )
        return PreparedMutation(
            action_type=self.action_type,
            plan=plan,
            preview_fields={
                "directive": directive_text,
                "account": account_name,
                "currency": currency,
                "open_date": open_date,
            },
            execution_spec={
                "account_name": account_name,
                "currency": currency,
                "open_date": open_date,
                "display_name": display_name,
            },
            display_kind="account_open_preview",
            display_summary="Open an account",
            display_diff=directive_text,
            validation_fields={"account": account_name},
            message="Account directive passed dry-run validation. Request explicit approval.",
        )


class MutationPreparationHandlerRegistry:
    """Explicit handler registration point for canonical preparation actions."""

    def __init__(self, handlers: tuple[MutationPreparationHandler, ...] | None = None) -> None:
        registered = handlers or (
            TransactionCommitPreparationHandler(),
            AccountOpenPreparationHandler(),
        )
        self._handlers = {handler.action_type: handler for handler in registered}

    def get(self, action_type: str) -> MutationPreparationHandler:
        return self._handlers[action_type]
