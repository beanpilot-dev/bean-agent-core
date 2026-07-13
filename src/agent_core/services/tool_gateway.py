"""Runtime-neutral tool execution gateway.

The gateway keeps approval policy out of LangGraph, SSE, and SaaS persistence.
Model-visible mutation tools call prepare_* methods and receive one of three
portable outcomes: completed, repairable_error, or approval_required. Trusted
hosts call apply_approved_action after human approval; that method is not part
of the model tool manifest.
"""

from dataclasses import asdict
from typing import Any, Callable

from agent_core.services.ledger import LedgerService, _digest_payload
from agent_core.services.types import (
    ApplyReceipt,
    ApprovalProof,
    ApprovalRequired,
    CommitResult,
    DependencyUnavailable,
    IntegrityFailed,
    InvariantViolation,
    LedgerConfig,
    PendingAction,
    QueryResult,
    ServiceResult,
    ToolApprovalRequired,
    ToolCompleted,
    ToolRepairableError,
    ValidationFailed,
)
from agent_core.services.workspace import GitService


class ToolExecutionGateway:
    """Normalize service-layer tool calls for SaaS, MCP, and local hosts."""

    def __init__(self, ledger_service: LedgerService | None = None) -> None:
        self._ledger = ledger_service or LedgerService()

    def normalize(self, tool_name: str, result: ServiceResult) -> ServiceResult:
        """Map service-specific results to portable tool outcomes."""
        if isinstance(result, (ApprovalRequired, PendingAction)):
            pending_action = asdict(result)
            return ToolApprovalRequired(
                tool_name=tool_name,
                action_type=result.action_type,
                pending_action=pending_action,
                display=result.display,
                validation=result.validation,
                policy=result.policy,
                message=result.message or "Approval is required before applying.",
            )

        if isinstance(result, (ValidationFailed, InvariantViolation)):
            return ToolRepairableError(
                tool_name=tool_name,
                error_type=result.status,
                message=getattr(result, "error", "") or getattr(result, "invariant", ""),
                remediation=getattr(result, "remediation", ""),
                result=asdict(result),
            )

        if isinstance(result, (CommitResult, ApplyReceipt, QueryResult)):
            return ToolCompleted(
                tool_name=tool_name,
                result=asdict(result),
            )

        return result

    def _prepare(
        self,
        tool_name: str,
        prepare: Callable[[], ServiceResult],
    ) -> ServiceResult:
        return self.normalize(tool_name, prepare())

    def prepare_commit(
        self,
        workspace: str,
        transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> ServiceResult:
        return self._prepare(
            "ledger_commit_transaction",
            lambda: self._ledger.prepare_commit(
                workspace,
                transaction_text,
                commit_message,
                whitelist,
                ledger_config,
            ),
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
    ) -> ServiceResult:
        return self._prepare(
            "ledger_update_transaction",
            lambda: self._ledger.prepare_update(
                workspace,
                target_date,
                narration,
                new_transaction_text,
                commit_message,
                whitelist,
                ledger_config,
            ),
        )

    def prepare_open(
        self,
        workspace: str,
        account_name: str,
        currency: str | None,
        open_date: str,
        display_name: str | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> ServiceResult:
        return self._prepare(
            "ledger_open_account",
            lambda: self._ledger.prepare_open(
                workspace,
                account_name,
                currency,
                open_date,
                display_name,
                ledger_config,
            ),
        )

    def prepare_bulk(
        self,
        workspace: str,
        transactions_text: str = "",
        commit_message: str = "",
        transactions_file: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> ServiceResult:
        return self._prepare(
            "ledger_import_transactions",
            lambda: self._ledger.prepare_bulk(
                workspace,
                transactions_text,
                commit_message,
                transactions_file,
                whitelist,
                ledger_config,
            ),
        )

    def prepare_change_set(
        self,
        workspace: str,
        operations: list[dict[str, Any]],
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> ServiceResult:
        return self._prepare(
            "ledger_prepare_change_set",
            lambda: self._ledger.prepare_change_set(
                workspace,
                operations,
                commit_message,
                whitelist,
                ledger_config,
            ),
        )

    def prepare_reconciliation(
        self,
        workspace: str,
        mode: str,
        assertion_date: str,
        account: str,
        amount: str,
        currency: str,
        pad_account: str | None = None,
        tolerance: str | None = None,
        commit_message: str = "",
        ledger_config: LedgerConfig | None = None,
    ) -> ServiceResult:
        return self._prepare(
            "ledger_prepare_reconciliation",
            lambda: self._ledger.prepare_reconciliation(
                workspace,
                mode,
                assertion_date,
                account,
                amount,
                currency,
                pad_account,
                tolerance,
                commit_message,
                ledger_config,
            ),
        )

    def apply_approved_action(
        self,
        *,
        workspace: str,
        pending_action: dict[str, Any],
        approval_proof: ApprovalProof | dict[str, Any],
        repo_url: str,
        git_service: GitService,
        github_token: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> ServiceResult:
        """Apply an immutable pending action after host-controlled approval."""
        proof = (
            approval_proof
            if isinstance(approval_proof, ApprovalProof)
            else ApprovalProof(
                approved_by=str(approval_proof.get("approved_by") or ""),
                approved_at=str(approval_proof.get("approved_at") or ""),
                approval_id=str(approval_proof.get("approval_id") or ""),
                pending_action_id=str(approval_proof.get("pending_action_id") or ""),
                payload_digest=str(approval_proof.get("payload_digest") or ""),
                integrity_digest=str(approval_proof.get("integrity_digest") or ""),
                host=str(approval_proof.get("host") or ""),
            )
        )
        if not proof.approved_by or not proof.approved_at or not proof.approval_id:
            return IntegrityFailed(
                pending_action_id=str(pending_action.get("pending_action_id") or ""),
                error="Approval proof is required before applying a pending action.",
            )
        pending_action_id = str(pending_action.get("pending_action_id") or "")
        pending_action_digest = str(pending_action.get("digest") or "")
        if (
            not proof.pending_action_id
            or not proof.payload_digest
            or proof.pending_action_id != pending_action_id
            or proof.payload_digest != _digest_payload(pending_action)
        ):
            return IntegrityFailed(
                pending_action_id=pending_action_id,
                error="Approval proof does not match the pending action payload.",
            )
        if proof.integrity_digest and proof.integrity_digest != pending_action_digest:
            return IntegrityFailed(
                pending_action_id=pending_action_id,
                error="Approval proof does not match the pending action digest.",
            )

        result = self._ledger.apply_pending_action(
            workspace,
            pending_action,
            repo_url,
            git_service,
            github_token,
            whitelist,
            ledger_config,
        )
        if isinstance(result, DependencyUnavailable | IntegrityFailed):
            return result
        return self.normalize("apply_approved_action", result)
