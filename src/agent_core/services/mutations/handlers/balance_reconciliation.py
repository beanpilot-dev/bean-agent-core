"""Balance-reconciliation preparation policy and plan construction."""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from ...queries import LedgerQueryService
from ...reconciliation import ReconciliationCalculator, format_decimal, is_valid_account_name
from ...types import InvariantViolation, LedgerConfig, QueryResult, ValidationFailed
from ..facts import (
    capture_account_state_fact,
    capture_balance_fact,
    capture_checkpoint_fact,
)
from ..planners import MutationPlanner
from .contracts import PreparedMutation


class BalanceReconciliationPreparationHandler:
    handler_key = "balance_reconciliation"

    def __init__(self, calculator: ReconciliationCalculator | None = None) -> None:
        self._calculator = calculator or ReconciliationCalculator()

    def build(
        self,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: Any,
    ) -> PreparedMutation | InvariantViolation | ValidationFailed:
        return self.build_reconciliation(
            workspace,
            observed_date=str(kwargs["observed_date"]),
            account=str(kwargs["account"]),
            amount=str(kwargs["amount"]),
            currency=str(kwargs["currency"]),
            adjustment_account=str(kwargs.get("adjustment_account") or ""),
            cutoff=str(kwargs.get("cutoff") or "end_of_day"),
            commit_message=str(kwargs.get("commit_message") or ""),
            allow_existing_checkpoint=False,
            include_assertion=True,
            checkpoint_update=False,
            ledger_config=ledger_config,
        )

    def build_reconciliation(
        self,
        workspace: str,
        *,
        observed_date: str,
        account: str,
        amount: str,
        currency: str,
        adjustment_account: str,
        cutoff: str,
        commit_message: str,
        allow_existing_checkpoint: bool,
        include_assertion: bool,
        checkpoint_update: bool,
        ledger_config: LedgerConfig | None,
    ) -> PreparedMutation | InvariantViolation | ValidationFailed:
        """Build the shared explicit-adjustment plan for normal and repair flows."""
        calculation = self._calculator.calculate_balance_adjustment(
            workspace, observed_date, account, amount, currency, cutoff, ledger_config
        )
        if not isinstance(calculation, QueryResult):
            return calculation
        if not is_valid_account_name(adjustment_account):
            return InvariantViolation(
                invariant="RECONCILIATION_ADJUSTMENT_ACCOUNT",
                severity="HARD",
                provided=adjustment_account,
                remediation="Provide an existing explicit adjustment account.",
            )
        existing_accounts = set(LedgerQueryService.get_accounts(workspace, ledger_config))
        if adjustment_account not in existing_accounts:
            return InvariantViolation(
                invariant="RECONCILIATION_ADJUSTMENT_ACCOUNT",
                severity="HARD",
                provided=adjustment_account,
                remediation="The adjustment account must already be open in the ledger.",
            )
        details = calculation.rows[0]
        assertion_date = str(details["assertion_date"])
        checkpoint_amount = self._calculator.existing_balance_assertion(
            workspace, assertion_date, account, currency, ledger_config
        )
        if checkpoint_amount is not None and not allow_existing_checkpoint:
            return InvariantViolation(
                invariant="RECONCILIATION_CHECKPOINT_EXISTS",
                severity="HARD",
                provided={"account": account, "assertion_date": assertion_date},
                remediation=(
                    "Use ledger_prepare_balance_update to repair this existing checkpoint; "
                    "it will not be replaced automatically."
                ),
            )

        target_amount = Decimal(amount)
        current_amount = Decimal(str(calculation.balance).split()[0])
        adjustment = target_amount - current_amount
        transaction_date = (date.fromisoformat(assertion_date) - timedelta(days=1)).isoformat()
        transaction_text = (
            f'{transaction_date} * "Balance reconciliation adjustment"\n'
            f"  {account}  {format_decimal(adjustment)} {currency}\n"
            f"  {adjustment_account}  {format_decimal(-adjustment)} {currency}"
        )
        assertion_text = (
            f"{assertion_date} balance {account}  {format_decimal(target_amount)} {currency}"
        )
        generated_text = (
            f"{transaction_text}\n\n{assertion_text}" if include_assertion else transaction_text
        )
        plan = MutationPlanner.reconciliation(
            generated_text, commit_message, checkpoint_update=checkpoint_update
        ).with_semantic_facts(
            tuple(
                dict.fromkeys(
                    (
                        capture_account_state_fact(workspace, account, ledger_config),
                        capture_account_state_fact(
                            workspace, adjustment_account, ledger_config
                        ),
                        capture_balance_fact(
                            workspace, account, assertion_date, ledger_config
                        ),
                        capture_checkpoint_fact(
                            workspace,
                            account,
                            assertion_date,
                            currency,
                            ledger_config,
                        ),
                    )
                )
            )
        )

        if checkpoint_update:
            title = "Balance checkpoint update"
            summary = (
                "Prepare an explicit adjustment that restores an existing balance checkpoint."
            )
            warning = (
                "This adds a new adjustment; it does not rewrite the earlier "
                "transaction or assertion."
            )
        else:
            title = "Balance reconciliation"
            summary = "Prepare an explicit adjustment transaction and balance assertion"
            warning = "Confirm that the unexplained difference is an intentional adjustment."

        current_balance = str(calculation.balance)
        target_balance = f"{format_decimal(target_amount)} {currency}"
        adjustment_text = f"{format_decimal(adjustment)} {currency}"
        return PreparedMutation(
            handler_key=self.handler_key,
            action_type="balance_reconciliation",
            plan=plan,
            preview_fields={
                "observed_date": observed_date,
                "cutoff": cutoff,
                "assertion_date": assertion_date,
                "account": account,
                "adjustment_account": adjustment_account,
                "currency": currency,
                "current_balance": current_balance,
                "target_balance": target_balance,
                "adjustment": adjustment_text,
                "assertion_status": "will_verify",
                "generated_text": generated_text,
            },
            execution_spec={
                "observed_date": observed_date,
                "cutoff": cutoff,
                "account": account,
                "amount": amount,
                "currency": currency,
                "adjustment_account": adjustment_account,
                "is_checkpoint_update": checkpoint_update,
                "commit_message": plan.commit_message,
            },
            display_fields={
                "kind": "balance_reconciliation_preview",
                "title": title,
                "summary": summary,
                "observed_date": observed_date,
                "cutoff": cutoff,
                "assertion_date": assertion_date,
                "current_balance": current_balance,
                "target_balance": target_balance,
                "adjustment": adjustment_text,
                "adjustment_account": adjustment_account,
                "assertion_status": "will_verify",
                "warning": warning,
                "generated_statements": generated_text,
                "diff": generated_text,
            },
            validation_fields={"account": account},
            message=(
                "Balance reconciliation passed dry-run validation. "
                "Request explicit approval."
            ),
            preview_target_field="target_file",
            validation_preview_fields=("target_file",),
            embed_preview_in_display=False,
        )
