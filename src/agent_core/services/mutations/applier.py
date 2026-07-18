"""Sidecar-only replay of ordered mutation operations."""

from ..types import LedgerConfig
from .persistence import SidecarMutationStore
from .plans import MutationPlan
from .sidecar import FilesystemSidecarMutationStore


class MutationApplier:
    """Apply a plan to a supplied workspace without validation or publishing.

    This is deliberately the only operation replay implementation.  Both the
    isolated validator and approved executor receive the same applier instance.
    """

    def __init__(self, store: SidecarMutationStore | None = None) -> None:
        self._store = store or FilesystemSidecarMutationStore()

    def apply(
        self, workspace: str, plan: MutationPlan, config: LedgerConfig | None = None
    ) -> tuple[str, ...]:
        touched: list[str] = []
        for operation in plan.operations:
            if operation.kind in {"append", "close", "price"}:
                target = self._store.append(workspace, operation.text, config)
            elif operation.kind == "open":
                target = self._store.open_directive(
                    workspace, operation.account_name or "", operation.text, config
                )
            elif operation.kind == "replace":
                if not operation.target_file or operation.old_text is None:
                    raise ValueError("Replace mutation plan is missing its precondition")
                target = self._store.replace(
                    workspace, operation.target_file, operation.old_text, operation.text, config
                )
            elif operation.kind == "delete":
                if not operation.target_file or operation.old_text is None:
                    raise ValueError("Delete mutation plan is missing its precondition")
                target = self._store.delete(
                    workspace,
                    operation.target_file,
                    operation.old_text,
                    operation.target_start_line,
                    config,
                )
            else:  # pragma: no cover - typed plans make this unreachable
                raise ValueError(f"Unsupported mutation operation: {operation.kind}")
            if target not in touched:
                touched.append(target)
        return tuple(touched)
