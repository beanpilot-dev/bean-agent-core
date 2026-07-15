"""Apply an approved pending action from its authoritative sealed plan."""

from dataclasses import asdict

from ..approvals.contracts import PendingActionService
from ..beancount import LedgerServiceError
from ..types import (
    ApplyReceipt,
    CommitResult,
    DependencyUnavailable,
    IntegrityFailed,
    InvariantViolation,
    LedgerConfig,
)
from ..workspace import GitService
from .executor import MutationExecutor
from .plans import MutationPlan
from .targets import sealed_write_set_matches
from .validator import validation_failure


def git_dependency_error(git: dict[str, object]) -> DependencyUnavailable | None:
    """Map repository publication output to the stable dependency error contract."""
    if not git["ok"]:
        return DependencyUnavailable(
            error=f"Written but git commit failed: {git['error']}",
        )
    push = git.get("push")
    if isinstance(push, str) and push.startswith("PUSH_FAILED"):
        return DependencyUnavailable(
            error=f"Git commit succeeded locally but push failed: {push}",
            retryable=True,
        )
    return None


class PendingActionApplicationService:
    """Verify and execute only the immutable plan approved by the host."""

    def __init__(self, executor: MutationExecutor | None = None) -> None:
        self._executor = executor or MutationExecutor()

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
        integrity = PendingActionService.verify_pending_action(action)
        if integrity:
            return integrity
        pending_action_id = str(action.get("pending_action_id") or "")
        action_type = str(action.get("action_type") or "")
        spec = action.get("execution_spec")
        if not isinstance(spec, dict):
            return IntegrityFailed(
                pending_action_id=pending_action_id,
                error="Pending action execution spec is invalid.",
            )
        raw_plan = spec.get("mutation_plan")
        if not isinstance(raw_plan, dict):
            return IntegrityFailed(
                pending_action_id=pending_action_id,
                error="Pending action mutation plan is required.",
            )
        try:
            plan = MutationPlan.from_spec(raw_plan)
        except ValueError:
            return IntegrityFailed(
                pending_action_id=pending_action_id,
                error="Pending action mutation plan is invalid.",
            )
        if not sealed_write_set_matches(plan, ledger_config):
            return IntegrityFailed(
                pending_action_id=pending_action_id,
                error=(
                    "Pending action write set no longer matches the active ledger layout. "
                    "Prepare and review a new action."
                ),
            )
        try:
            _touched, git, output = self._executor.apply_and_publish(
                workspace,
                plan,
                repo_url,
                git_service,
                github_token,
                ledger_config,
            )
        except ValueError:
            return IntegrityFailed(
                pending_action_id=pending_action_id,
                error="Pending action mutation plan violates sidecar write isolation.",
            )
        except OSError as exc:
            raise LedgerServiceError("Ledger mutation apply workspace unavailable") from exc
        if output == "MUTATION_PRECONDITION_FAILED":
            return InvariantViolation(
                invariant="MUTATION_PRECONDITION_FAILED",
                severity="HARD",
                remediation="The ledger changed after preview. Prepare and review a new action.",
            )
        if output == "MUTATION_PLAN_WRITE_SET_MISMATCH":
            return IntegrityFailed(
                pending_action_id=pending_action_id,
                error=(
                    "Pending action write set no longer matches the active ledger layout. "
                    "Prepare and review a new action."
                ),
            )
        if output:
            return validation_failure(output, plan.remediation)
        if dependency_error := git_dependency_error(git):
            return dependency_error
        return ApplyReceipt(
            pending_action_id=pending_action_id,
            action_type=action_type,
            receipt=asdict(
                CommitResult(
                    outcome="Approved mutation plan validated and committed",
                    result={"plan_version": plan.schema_version},
                    push_status=git["push"],
                )
            ),
        )
