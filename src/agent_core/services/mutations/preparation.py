"""Shared orchestration shell for approval-gated mutation preparation."""

import uuid
from dataclasses import asdict

from ..approvals.contracts import PendingActionService
from ..ledger_paths import sidecar_target_file
from ..types import (
    InvariantViolation,
    LedgerConfig,
    PendingAction,
    Preview,
    ValidationFailed,
)
from .coordinator import MutationCoordinator
from .handlers import MutationPreparationHandlerRegistry, PreparedMutation
from .plans import MutationPlan
from .validator import MutationValidator, PlanValidation

PreparationFailure = InvariantViolation | ValidationFailed


class MutationPreparationService:
    """Dispatch handlers, validate plans, seal them, and create approvals."""

    def __init__(
        self,
        handler_registry: MutationPreparationHandlerRegistry | None = None,
        validator: MutationValidator | None = None,
    ) -> None:
        self._handler_registry = handler_registry or MutationPreparationHandlerRegistry()
        self._validator = validator or MutationValidator()

    @staticmethod
    def _serialized_plan(
        workspace: str, plan: MutationPlan, ledger_config: LedgerConfig | None
    ) -> dict[str, object]:
        """Seal the exact approved operation list with workspace preconditions."""
        return MutationCoordinator.seal(workspace, plan, ledger_config).to_spec()

    @staticmethod
    def _materialize_preview(
        prepared: PreparedMutation,
        validation: PlanValidation,
        ledger_config: LedgerConfig | None,
    ) -> Preview:
        fields = dict(prepared.preview_fields)
        if prepared.preview_target_field:
            fields[prepared.preview_target_field] = (
                validation.touched_files[-1]
                if validation.touched_files
                else sidecar_target_file(ledger_config)
            )
        fields["validation"] = asdict(validation.validation)
        return Preview(
            proposal_id=f"prop_{uuid.uuid4().hex[:12]}",
            operation=prepared.action_type,
            preview=fields,
            message=prepared.message,
        )

    @staticmethod
    def _materialize_pending_action(
        prepared: PreparedMutation,
        sealed_plan: dict[str, object],
        preview: Preview,
    ) -> PendingAction:
        display = dict(prepared.display_fields)
        if prepared.embed_preview_in_display:
            display["preview"] = preview.preview
        validation = dict(prepared.validation_fields)
        for field_name in prepared.validation_preview_fields:
            validation[field_name] = preview.preview.get(field_name)
        validation["status"] = "validated"
        validation["dry_run"] = preview.preview.get("validation")
        return PendingActionService.create_pending_action(
            action_type=prepared.action_type,
            execution_spec={"mutation_plan": sealed_plan, **prepared.execution_spec},
            display=display,
            validation=validation,
        )

    def _build(
        self,
        handler_key: str,
        workspace: str,
        ledger_config: LedgerConfig | None,
        **kwargs: object,
    ) -> PreparedMutation | PreparationFailure:
        return self._handler_registry.get(handler_key).build(
            workspace, ledger_config, **kwargs
        )

    def _validate(
        self,
        workspace: str,
        prepared: PreparedMutation,
        ledger_config: LedgerConfig | None,
    ) -> PlanValidation | ValidationFailed:
        validation = self._validator.validate(workspace, prepared.plan, ledger_config)
        return validation.failure or validation

    def _preview_registered(
        self,
        handler_key: str,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: object,
    ) -> Preview | PreparationFailure:
        prepared = self._build(handler_key, workspace, ledger_config, **kwargs)
        if not isinstance(prepared, PreparedMutation):
            return prepared
        validation = self._validate(workspace, prepared, ledger_config)
        if isinstance(validation, ValidationFailed):
            return validation
        return self._materialize_preview(prepared, validation, ledger_config)

    def _prepare_registered(
        self,
        handler_key: str,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: object,
    ) -> PendingAction | PreparationFailure:
        prepared = self._build(handler_key, workspace, ledger_config, **kwargs)
        if not isinstance(prepared, PreparedMutation):
            return prepared
        validation = self._validate(workspace, prepared, ledger_config)
        if isinstance(validation, ValidationFailed):
            return validation
        preview = self._materialize_preview(prepared, validation, ledger_config)
        sealed_plan = self._serialized_plan(workspace, prepared.plan, ledger_config)
        return self._materialize_pending_action(prepared, sealed_plan, preview)

    def preview_commit(
        self,
        workspace: str,
        transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> Preview | PreparationFailure:
        return self._preview_registered(
            "commit_transaction",
            workspace,
            ledger_config,
            transaction_text=transaction_text,
            commit_message=commit_message,
            whitelist=whitelist,
        )

    def prepare_commit(
        self,
        workspace: str,
        transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> PendingAction | PreparationFailure:
        return self._prepare_registered(
            "commit_transaction",
            workspace,
            ledger_config,
            transaction_text=transaction_text,
            commit_message=commit_message,
            whitelist=whitelist,
        )

    def preview_open(
        self,
        workspace: str,
        account_name: str,
        currency: str | None,
        open_date: str,
        display_name: str | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> Preview | PreparationFailure:
        return self._preview_registered(
            "open_account",
            workspace,
            ledger_config,
            account_name=account_name,
            currency=currency,
            open_date=open_date,
            display_name=display_name,
        )

    def prepare_open(
        self,
        workspace: str,
        account_name: str,
        currency: str | None,
        open_date: str,
        display_name: str | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> PendingAction | PreparationFailure:
        return self._prepare_registered(
            "open_account",
            workspace,
            ledger_config,
            account_name=account_name,
            currency=currency,
            open_date=open_date,
            display_name=display_name,
        )

    def preview_transaction_update(
        self,
        workspace: str,
        transaction_ref: str,
        revision_fingerprint: str,
        new_transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> Preview | PreparationFailure:
        return self._preview_registered(
            "update_transaction",
            workspace,
            ledger_config,
            transaction_ref=transaction_ref,
            revision_fingerprint=revision_fingerprint,
            new_transaction_text=new_transaction_text,
            commit_message=commit_message,
            whitelist=whitelist,
        )

    def prepare_transaction_update(
        self,
        workspace: str,
        transaction_ref: str,
        revision_fingerprint: str,
        new_transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> PendingAction | PreparationFailure:
        return self._prepare_registered(
            "update_transaction",
            workspace,
            ledger_config,
            transaction_ref=transaction_ref,
            revision_fingerprint=revision_fingerprint,
            new_transaction_text=new_transaction_text,
            commit_message=commit_message,
            whitelist=whitelist,
        )

    def preview_transaction_delete(
        self,
        workspace: str,
        transaction_ref: str,
        revision_fingerprint: str,
        commit_message: str,
        ledger_config: LedgerConfig | None = None,
    ) -> Preview | PreparationFailure:
        return self._preview_registered(
            "delete_transaction",
            workspace,
            ledger_config,
            transaction_ref=transaction_ref,
            revision_fingerprint=revision_fingerprint,
            commit_message=commit_message,
        )

    def prepare_transaction_delete(
        self,
        workspace: str,
        transaction_ref: str,
        revision_fingerprint: str,
        commit_message: str,
        ledger_config: LedgerConfig | None = None,
    ) -> PendingAction | PreparationFailure:
        return self._prepare_registered(
            "delete_transaction",
            workspace,
            ledger_config,
            transaction_ref=transaction_ref,
            revision_fingerprint=revision_fingerprint,
            commit_message=commit_message,
        )

    def preview_bulk(
        self,
        workspace: str,
        transactions_text: str = "",
        commit_message: str = "",
        transactions_file: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> Preview | PreparationFailure:
        return self._preview_registered(
            "bulk_commit",
            workspace,
            ledger_config,
            transactions_text=transactions_text,
            commit_message=commit_message,
            transactions_file=transactions_file,
            whitelist=whitelist,
        )

    def prepare_bulk(
        self,
        workspace: str,
        transactions_text: str = "",
        commit_message: str = "",
        transactions_file: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> PendingAction | PreparationFailure:
        return self._prepare_registered(
            "bulk_commit",
            workspace,
            ledger_config,
            transactions_text=transactions_text,
            commit_message=commit_message,
            transactions_file=transactions_file,
            whitelist=whitelist,
        )

    def prepare_change_set(
        self,
        workspace: str,
        operations: list[dict[str, object]],
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> PendingAction | PreparationFailure:
        return self._prepare_registered(
            "change_set",
            workspace,
            ledger_config,
            operations=operations,
            commit_message=commit_message,
            whitelist=whitelist,
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
    ) -> Preview | PreparationFailure:
        return self._preview_registered(
            "balance_reconciliation",
            workspace,
            ledger_config,
            observed_date=observed_date,
            account=account,
            amount=amount,
            currency=currency,
            adjustment_account=adjustment_account,
            cutoff=cutoff,
            commit_message=commit_message,
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
    ) -> PendingAction | PreparationFailure:
        return self._prepare_registered(
            "balance_reconciliation",
            workspace,
            ledger_config,
            observed_date=observed_date,
            account=account,
            amount=amount,
            currency=currency,
            adjustment_account=adjustment_account,
            cutoff=cutoff,
            commit_message=commit_message,
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
    ) -> PendingAction | PreparationFailure:
        return self._prepare_registered(
            "balance_update",
            workspace,
            ledger_config,
            assertion_date=assertion_date,
            account=account,
            currency=currency,
            adjustment_account=adjustment_account,
            commit_message=commit_message,
        )
