"""Sidecar-only replay of ordered mutation operations."""

from ..types import LedgerConfig
from . import sidecar
from .plans import MutationPlan


class MutationApplier:
    """Apply a plan to a supplied workspace without validation or publishing.

    This is deliberately the only operation replay implementation.  Both the
    isolated validator and approved executor receive the same applier instance.
    """

    def apply(
        self, workspace: str, plan: MutationPlan, config: LedgerConfig | None = None
    ) -> tuple[str, ...]:
        touched: list[str] = []
        for operation in plan.operations:
            if operation.kind == "append":
                target = sidecar.append(workspace, operation.text, config)
            elif operation.kind == "open":
                target = sidecar.open_directive(
                    workspace, operation.account_name or "", operation.text, config
                )
            elif operation.kind == "replace":
                if not operation.target_file or operation.old_text is None:
                    raise ValueError("Replace mutation plan is missing its precondition")
                target = sidecar.replace(
                    workspace, operation.target_file, operation.old_text, operation.text, config
                )
            else:  # pragma: no cover - typed plans make this unreachable
                raise ValueError(f"Unsupported mutation operation: {operation.kind}")
            if target not in touched:
                touched.append(target)
        return tuple(touched)
