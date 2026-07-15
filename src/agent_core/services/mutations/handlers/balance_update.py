"""Existing balance-checkpoint repair preparation."""

from dataclasses import replace
from datetime import date, timedelta
from typing import Any

from ...reconciliation import ReconciliationCalculator, format_decimal
from ...types import InvariantViolation, LedgerConfig, ValidationFailed
from .balance_reconciliation import BalanceReconciliationPreparationHandler
from .contracts import PreparedMutation


class BalanceUpdatePreparationHandler:
    handler_key = "balance_update"

    def __init__(self, calculator: ReconciliationCalculator | None = None) -> None:
        self._calculator = calculator or ReconciliationCalculator()
        self._reconciliation = BalanceReconciliationPreparationHandler(self._calculator)

    def build(
        self,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: Any,
    ) -> PreparedMutation | InvariantViolation | ValidationFailed:
        assertion_date = str(kwargs["assertion_date"])
        account = str(kwargs["account"])
        currency = str(kwargs["currency"])
        adjustment_account = str(kwargs["adjustment_account"])
        commit_message = str(kwargs.get("commit_message") or "")
        checkpoint_amount = self._calculator.existing_balance_assertion(
            workspace, assertion_date, account, currency, ledger_config
        )
        if checkpoint_amount is None:
            return InvariantViolation(
                invariant="RECONCILIATION_CHECKPOINT_NOT_FOUND",
                severity="HARD",
                provided={"account": account, "assertion_date": assertion_date},
                remediation=(
                    "Provide the account, currency, and assertion date of an "
                    "existing balance checkpoint."
                ),
            )
        observed_date = (date.fromisoformat(assertion_date) - timedelta(days=1)).isoformat()
        prepared = self._reconciliation.build_reconciliation(
            workspace,
            observed_date=observed_date,
            account=account,
            amount=format_decimal(checkpoint_amount),
            currency=currency,
            adjustment_account=adjustment_account,
            cutoff="end_of_day",
            commit_message=commit_message,
            allow_existing_checkpoint=True,
            include_assertion=False,
            checkpoint_update=True,
            ledger_config=ledger_config,
        )
        if isinstance(prepared, PreparedMutation):
            return replace(prepared, handler_key=self.handler_key)
        return prepared
