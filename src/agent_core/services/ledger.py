"""Compatibility facade over focused ledger preparation, apply, and read services."""

from .beancount import Beancount, LedgerServiceError
from .inspection import preflight_report
from .mutations.application import (
    PendingActionApplicationService,
    git_dependency_error,
)
from .mutations.handlers import extract_posting_accounts, validate_posting_accounts
from .mutations.preparation import MutationPreparationService
from .reconciliation import ReconciliationCalculator
from .transaction_locator import TransactionLocator
from .types import LedgerConfig
from .workspace import GitService

_git_dependency_error = git_dependency_error

__all__ = ["Beancount", "LedgerService", "LedgerServiceError", "_git_dependency_error"]


class LedgerService:
    """Retain stable callers while composing focused deterministic services."""

    def __init__(self) -> None:
        self._preparation = MutationPreparationService()
        self._application = PendingActionApplicationService()
        self._reconciliation = ReconciliationCalculator()

    def preview_commit(
        self,
        workspace: str,
        transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ):
        return self._preparation.preview_commit(
            workspace, transaction_text, commit_message, whitelist, ledger_config
        )

    def prepare_commit(
        self,
        workspace: str,
        transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ):
        return self._preparation.prepare_commit(
            workspace, transaction_text, commit_message, whitelist, ledger_config
        )

    def preview_open(
        self,
        workspace: str,
        account_name: str,
        currency: str | None,
        open_date: str,
        display_name: str | None = None,
        ledger_config: LedgerConfig | None = None,
    ):
        return self._preparation.preview_open(
            workspace,
            account_name,
            currency,
            open_date,
            display_name,
            ledger_config,
        )

    def prepare_open(
        self,
        workspace: str,
        account_name: str,
        currency: str | None,
        open_date: str,
        display_name: str | None = None,
        ledger_config: LedgerConfig | None = None,
    ):
        return self._preparation.prepare_open(
            workspace,
            account_name,
            currency,
            open_date,
            display_name,
            ledger_config,
        )

    def preview_update(
        self,
        workspace: str,
        target_date: str,
        narration: str,
        new_transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ):
        return self._preparation.preview_update(
            workspace,
            target_date,
            narration,
            new_transaction_text,
            commit_message,
            whitelist,
            ledger_config,
        )

    def prepare_update(
        self,
        workspace: str,
        target_date: str,
        narration: str,
        new_transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ):
        return self._preparation.prepare_update(
            workspace,
            target_date,
            narration,
            new_transaction_text,
            commit_message,
            whitelist,
            ledger_config,
        )

    def preview_bulk(
        self,
        workspace: str,
        transactions_text: str = "",
        commit_message: str = "",
        transactions_file: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ):
        return self._preparation.preview_bulk(
            workspace,
            transactions_text,
            commit_message,
            transactions_file,
            whitelist,
            ledger_config,
        )

    def prepare_bulk(
        self,
        workspace: str,
        transactions_text: str = "",
        commit_message: str = "",
        transactions_file: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ):
        return self._preparation.prepare_bulk(
            workspace,
            transactions_text,
            commit_message,
            transactions_file,
            whitelist,
            ledger_config,
        )

    def prepare_change_set(
        self,
        workspace: str,
        operations: list[dict[str, object]],
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ):
        return self._preparation.prepare_change_set(
            workspace, operations, commit_message, whitelist, ledger_config
        )

    def preview_balance_reconciliation(
        self,
        workspace: str,
        observed_date: str,
        account: str,
        amount: str,
        currency: str,
        adjustment_account: str,
        cutoff: str = "end_of_day",
        commit_message: str = "",
        ledger_config: LedgerConfig | None = None,
    ):
        return self._preparation.preview_balance_reconciliation(
            workspace,
            observed_date,
            account,
            amount,
            currency,
            adjustment_account,
            cutoff,
            commit_message,
            ledger_config,
        )

    def prepare_balance_reconciliation(
        self,
        workspace: str,
        observed_date: str,
        account: str,
        amount: str,
        currency: str,
        adjustment_account: str = "",
        cutoff: str = "end_of_day",
        commit_message: str = "",
        ledger_config: LedgerConfig | None = None,
    ):
        return self._preparation.prepare_balance_reconciliation(
            workspace,
            observed_date,
            account,
            amount,
            currency,
            adjustment_account,
            cutoff,
            commit_message,
            ledger_config,
        )

    def prepare_balance_update(
        self,
        workspace: str,
        assertion_date: str,
        account: str,
        currency: str,
        adjustment_account: str,
        commit_message: str = "",
        ledger_config: LedgerConfig | None = None,
    ):
        return self._preparation.prepare_balance_update(
            workspace,
            assertion_date,
            account,
            currency,
            adjustment_account,
            commit_message,
            ledger_config,
        )

    def apply_pending_action(
        self,
        workspace: str,
        action: dict[str, object],
        repo_url: str,
        git_service: GitService,
        github_token: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ):
        return self._application.apply_pending_action(
            workspace,
            action,
            repo_url,
            git_service,
            github_token,
            whitelist,
            ledger_config,
        )

    def calculate_balance_adjustment(
        self,
        workspace: str,
        observed_date: str,
        account: str,
        amount: str,
        currency: str,
        cutoff: str = "end_of_day",
        ledger_config: LedgerConfig | None = None,
    ):
        return self._reconciliation.calculate_balance_adjustment(
            workspace,
            observed_date,
            account,
            amount,
            currency,
            cutoff,
            ledger_config,
        )

    @staticmethod
    def _extract_accounts(transaction_text: str) -> list[str]:
        return extract_posting_accounts(transaction_text)

    validate_accounts = staticmethod(validate_posting_accounts)

    @staticmethod
    def find_transaction_block(
        workspace: str,
        target_date: str,
        narration: str,
        ledger_config: LedgerConfig | None = None,
    ) -> list[tuple[str, str, str]]:
        return [
            (match.relative_path, match.file_content, match.block)
            for match in TransactionLocator.find(
                workspace, target_date, narration, ledger_config
            )
        ]

    preflight_report = staticmethod(preflight_report)
