"""Coordinate freshly prepared approvals and inspect persisted batches without publishing."""

from dataclasses import asdict
from typing import Any

from ..approvals.contracts import PendingActionService
from ..types import LedgerConfig
from .coordinator import MutationCoordinator
from .executor import MutationExecutor
from .plans import MutationPlan
from .targets import sealed_write_set_matches
from .validator import MutationValidator


def _verified_plan(action: dict[str, Any], config: LedgerConfig | None) -> MutationPlan:
    if PendingActionService.verify_pending_action(action):
        raise ValueError("Invalid approval contract")
    spec = action.get("execution_spec")
    raw = spec.get("mutation_plan") if isinstance(spec, dict) else None
    if not isinstance(raw, dict):
        raise ValueError("Missing sealed mutation plan")
    plan = MutationPlan.from_spec(raw)
    if not sealed_write_set_matches(plan, config):
        raise ValueError("Invalid sealed write set")
    return plan


def check_independent_batch(
    actions: list[dict[str, Any]], config: LedgerConfig | None = None
) -> str | None:
    """Fail closed before the first publish when sibling plans share state.

    A guard never combines or reseals contracts that have already been reviewed.
    Legacy semantic read sets may include files outside their write sets, so
    disjoint writes alone are insufficient to prove batch independence.
    """
    try:
        plans = [_verified_plan(action, config) for action in actions]
    except (TypeError, ValueError):
        return "INTEGRITY_FAILED"
    for index, plan in enumerate(plans):
        writes = {condition.path for condition in plan.preconditions}
        for sibling in plans[index + 1 :]:
            sibling_writes = {condition.path for condition in sibling.preconditions}
            if writes & sibling_writes:
                return "BATCH_REPREPARE_REQUIRED"
            # Non-file semantic facts can depend on any ledger mutation. A
            # standalone fresh preview is the safe fallback for such groups.
            if any(
                fact.kind != "included_file_digest"
                for fact in (*plan.semantic_facts, *sibling.semantic_facts)
            ):
                return "BATCH_REPREPARE_REQUIRED"
            if writes & {fact.subject for fact in sibling.semantic_facts} or sibling_writes & {
                fact.subject for fact in plan.semantic_facts
            }:
                return "BATCH_REPREPARE_REQUIRED"
    return None


def coordinate_prepared_actions(
    workspace: str,
    actions: list[dict[str, Any]],
    config: LedgerConfig | None = None,
) -> list[dict[str, Any]]:
    """Seal one ordered plan before same-turn proposals reach user review.

    Only native append/open/close proposals can be combined here. Historical
    edits, deletions and other specialized policies keep their own contracts.
    """
    if len(actions) < 2 or any(
        action.get("action_type")
        not in {"open_account", "commit_transaction", "change_set", "close_account"}
        for action in actions
    ):
        return actions
    plans = [_verified_plan(action, config) for action in actions]
    if any(
        operation.kind not in {"open", "append", "close"}
        for plan in plans
        for operation in plan.operations
    ):
        return actions
    if any(not MutationExecutor.preconditions_hold(workspace, plan, config) for plan in plans):
        raise ValueError("Prepared group is stale")
    plan = MutationPlan.from_operations(
        [operation for member in plans for operation in member.operations],
        commit_message="Apply approved ledger change set",
        remediation="Prepare and review a new change set.",
    ).with_semantic_facts(
        tuple(dict.fromkeys(fact for member in plans for fact in member.semantic_facts))
    )
    validation = MutationValidator().validate(workspace, plan, config)
    if validation.failure:
        raise ValueError("Combined change set failed validation")
    sealed = MutationCoordinator.seal(workspace, plan, config)
    items: list[dict[str, Any]] = []
    for action in actions:
        display = action.get("display") or {}
        members = display.get("items")
        if isinstance(members, list):
            items.extend(dict(member) for member in members)
        else:
            items.append(
                {
                    "type": action["action_type"],
                    "summary": display.get("summary", ""),
                    "diff": display.get("diff", ""),
                }
            )
    for index, item in enumerate(items):
        item["operation_index"] = index
    continuations = [action for action in actions if action.get("continue_after_approval")]
    pending = PendingActionService.create_pending_action(
        action_type="change_set",
        execution_spec={"mutation_plan": sealed.to_spec()},
        display={
            "kind": "change_set_preview",
            "summary": f"Apply {len(plan.operations)} related ledger changes",
            "diff": "\n\n".join(operation.text for operation in plan.operations),
            "items": items,
        },
        validation={
            "status": "validated",
            "dry_run": asdict(validation.validation),
            "operation_count": len(plan.operations),
            "transaction_count": sum(operation.kind == "append" for operation in plan.operations),
            "account_closure_count": sum(
                operation.kind == "close" for operation in plan.operations
            ),
        },
        continue_after_approval=bool(continuations),
        continuation_reason="; ".join(
            str(action.get("continuation_reason") or "") for action in continuations
        ),
        next_intent_summary="; ".join(
            str(action.get("next_intent_summary") or "") for action in continuations
        ),
    )
    return [asdict(pending)]
