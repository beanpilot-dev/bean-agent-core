"""Apply an approved pending action from its authoritative sealed plan."""

from dataclasses import asdict
from datetime import datetime, timezone

from ..approvals.contracts import PendingActionService, verify_approval_proof
from ..beancount import LedgerServiceError
from ..types import (
    ApplyReceipt,
    ApprovalProof,
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
        approval_proof: ApprovalProof | dict[str, object] | None = None,
    ):
        integrity = PendingActionService.verify_pending_action(action)
        if integrity:
            return integrity
        pending_action_id = str(action.get("pending_action_id") or "")
        action_type = str(action.get("action_type") or "")
        approval_integrity = verify_approval_proof(action, approval_proof)
        if approval_integrity:
            return approval_integrity
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
        commit_sha = git.get("commit_sha") if isinstance(git.get("commit_sha"), str) else None
        audit = _sanitized_application_audit(
            action,
            action_type,
            pending_action_id,
            approval_proof,
            commit_sha,
        )
        return ApplyReceipt(
            pending_action_id=pending_action_id,
            action_type=action_type,
            receipt=asdict(
                CommitResult(
                    outcome="Approved mutation plan validated and committed",
                    result={
                        "plan_version": plan.schema_version,
                        **({"audit": audit} if audit else {}),
                    },
                    push_status=git["push"],
                    commit_sha=(
                        git.get("commit_sha")
                        if isinstance(git.get("commit_sha"), str)
                        else None
                    ),
                )
            ),
        )


def _sanitized_application_audit(
    action: dict[str, object],
    action_type: str,
    pending_action_id: str,
    approval_proof: ApprovalProof | dict[str, object] | None,
    commit_sha: str | None,
) -> dict[str, object] | None:
    """Return only non-content facts suitable for an apply receipt."""
    if action_type != "delete_transaction":
        return None
    display = action.get("display")
    if not isinstance(display, dict):
        display = {}
    audit: dict[str, object] = {
        "pending_action_id": pending_action_id,
        "action_type": action_type,
        "deletion_classification": "high_risk_transaction_deletion",
    }
    for key in ("transaction_ref", "revision_fingerprint"):
        value = display.get(key)
        if isinstance(value, str) and value:
            audit[key] = value
    if isinstance(approval_proof, ApprovalProof):
        proof = approval_proof
    elif isinstance(approval_proof, dict):
        proof = ApprovalProof(
            approved_by=str(approval_proof.get("approved_by") or ""),
            approved_at=str(approval_proof.get("approved_at") or ""),
            approval_id=str(approval_proof.get("approval_id") or ""),
            pending_action_id=str(approval_proof.get("pending_action_id") or ""),
            payload_digest=str(approval_proof.get("payload_digest") or ""),
            integrity_digest=str(approval_proof.get("integrity_digest") or ""),
            host=str(approval_proof.get("host") or ""),
        )
    else:
        proof = None
    if proof:
        audit.update(
            {
                "approved_by": proof.approved_by,
                "approved_at": proof.approved_at,
                "approval_id": proof.approval_id,
                "host": proof.host,
            }
        )
    if commit_sha:
        audit["commit_sha"] = commit_sha
    audit["resolution_completed_at"] = datetime.now(timezone.utc).isoformat()
    return audit
