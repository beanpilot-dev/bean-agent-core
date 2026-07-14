"""Apply sealed pending-action plans and characterize legacy fallback dispatch."""

from dataclasses import asdict
from typing import TYPE_CHECKING

from ..approvals.contracts import PendingActionService
from ..types import ApplyReceipt, CommitResult, IntegrityFailed
from ..workspace import GitService
from .plans import MutationPlan

if TYPE_CHECKING:
    from .preparation import MutationPreparationService


class PendingActionApplicationService:
    """Own the deterministic approved-application boundary.

    The signed plan is always preferred.  The explicit fallback is retained
    solely for actions persisted before sealed plans were introduced.
    """

    def apply_pending_action(
        self,
        preparation: "MutationPreparationService",
        workspace: str,
        action: dict[str, object],
        repo_url: str,
        git_service: GitService,
        github_token: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config=None,
    ):
        from .preparation import _apply_plan, _git_dependency_error

        integrity = PendingActionService.verify_pending_action(action)
        if integrity:
            return integrity
        action_type = str(action.get("action_type") or "")
        spec = action.get("execution_spec")
        if not isinstance(spec, dict):
            return IntegrityFailed(
                pending_action_id=str(action.get("pending_action_id") or ""),
                error="Pending action execution spec is invalid.",
            )

        raw_plan = spec.get("mutation_plan")
        if isinstance(raw_plan, dict):
            try:
                plan = MutationPlan.from_spec(raw_plan)
            except ValueError:
                return IntegrityFailed(
                    pending_action_id=str(action.get("pending_action_id") or ""),
                    error="Pending action mutation plan is invalid.",
                )
            _, git, failure = _apply_plan(
                workspace, ledger_config, plan, repo_url, git_service, github_token
            )
            if failure:
                return failure
            if dependency_error := _git_dependency_error(git):
                return dependency_error
            return ApplyReceipt(
                pending_action_id=str(action.get("pending_action_id") or ""),
                action_type=action_type,
                receipt=asdict(
                    CommitResult(
                        outcome="Approved mutation plan validated and committed",
                        result={"plan_version": plan.schema_version},
                        push_status=git["push"],
                    )
                ),
            )

        result = self._apply_legacy(
            preparation, action_type, spec, workspace, repo_url, git_service,
            github_token, whitelist, ledger_config,
        )
        if isinstance(result, CommitResult):
            return ApplyReceipt(
                pending_action_id=str(action.get("pending_action_id") or ""),
                action_type=action_type,
                receipt=asdict(result),
            )
        return result

    @staticmethod
    def _apply_legacy(
        preparation: "MutationPreparationService", action_type: str, spec: dict[str, object],
        workspace: str, repo_url: str, git_service: GitService, github_token: str | None,
        whitelist: list[str] | None, ledger_config,
    ):
        if action_type == "commit_transaction":
            return preparation.confirm_commit(
                workspace,
                str(spec.get("transaction_text") or ""),
                str(spec.get("commit_message") or ""),
                repo_url,
                git_service,
                github_token,
                whitelist,
                ledger_config,
            )
        if action_type == "open_account":
            return preparation.confirm_open(
                workspace,
                str(spec.get("account_name") or ""),
                spec.get("currency") if isinstance(spec.get("currency"), str) else None,
                str(spec.get("open_date") or ""),
                repo_url,
                git_service,
                spec.get("display_name") if isinstance(spec.get("display_name"), str) else None,
                github_token,
                ledger_config,
            )
        if action_type == "update_transaction":
            return preparation.confirm_update(
                workspace,
                str(spec.get("target_date") or ""),
                str(spec.get("narration") or ""),
                str(spec.get("new_transaction_text") or ""),
                str(spec.get("commit_message") or ""),
                repo_url,
                git_service,
                github_token,
                whitelist,
                ledger_config,
            )
        if action_type == "bulk_commit":
            return preparation.confirm_bulk(
                workspace,
                str(spec.get("transactions_text") or ""),
                str(spec.get("commit_message") or ""),
                repo_url,
                git_service,
                None,
                github_token,
                whitelist,
                ledger_config,
            )
        if action_type == "change_set":
            operations = spec.get("operations")
            if not isinstance(operations, list) or not all(
                isinstance(item, dict) for item in operations
            ):
                return IntegrityFailed(
                    pending_action_id="", error="Change-set operations are invalid."
                )
            return preparation.confirm_change_set(
                workspace,
                operations,
                str(spec.get("commit_message") or ""),
                repo_url,
                git_service,
                github_token,
                whitelist,
                ledger_config,
            )
        if action_type == "balance_reconciliation":
            return preparation.confirm_balance_reconciliation(
                workspace,
                str(spec.get("observed_date") or ""),
                str(spec.get("account") or ""),
                str(spec.get("amount") or ""),
                str(spec.get("currency") or ""),
                repo_url,
                git_service,
                str(spec.get("adjustment_account") or ""),
                str(spec.get("cutoff") or "end_of_day"),
                spec.get("is_checkpoint_update") is True,
                str(spec.get("commit_message") or ""),
                github_token,
                ledger_config,
            )
        return IntegrityFailed(
            pending_action_id="", error=f"Unsupported pending action type: {action_type}"
        )
