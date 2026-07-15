"""Account-open preparation policy and plan construction."""

import re
from typing import Any

from ...queries import LedgerQueryService
from ...types import InvariantViolation, LedgerConfig
from ..facts import capture_account_state_fact
from ..planners import MutationPlanner
from .contracts import PreparedMutation

_ACCOUNT_NAME_RE = re.compile(
    r"^(Assets|Liabilities|Equity|Income|Expenses)(:[A-Z][A-Za-z0-9\-]+)+$"
)


class AccountOpenPreparationHandler:
    handler_key = "open_account"

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
            handler_key=self.handler_key,
            action_type="open_account",
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
            display_fields={
                "kind": "account_open_preview",
                "summary": "Open an account",
                "diff": directive_text,
            },
            validation_fields={"account": account_name},
            message="Account directive passed dry-run validation. Request explicit approval.",
            preview_target_field="target_file",
        )
