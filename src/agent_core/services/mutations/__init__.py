"""Shared deterministic ledger mutation planning and execution primitives.

This package deliberately has no dependency on the workflow or pending-action
layers.  Callers construct plans from approved inputs and use the coordinator
for both dry-run validation and durable application.
"""

from .applier import MutationApplier
from .coordinator import MutationCoordinator
from .executor import (
    BeancountLedgerFormatter,
    LedgerFormatter,
    MutationExecutor,
)
from .persistence import SidecarMutationStore, SidecarSnapshot
from .planners import MutationPlanner
from .plans import MutationOperation, MutationPlan
from .preparation import MutationPreparationService
from .publisher import (
    MutationCommitMarker,
    PublicationReconciliation,
    PublishReceipt,
    PublishRequest,
    ReconciledRepositoryPublisher,
    RepositoryPublisher,
)
from .sidecar import FilesystemSidecarMutationStore
from .targets import potential_write_targets, sealed_write_set_matches
from .validator import (
    BeancountLedgerValidator,
    LedgerValidator,
    MutationValidator,
    PlanValidation,
    summarize_validation_failure,
    validation_failure,
)

__all__ = [
    "MutationCoordinator",
    "MutationApplier",
    "MutationValidator",
    "MutationExecutor",
    "PlanValidation",
    "LedgerValidator",
    "BeancountLedgerValidator",
    "summarize_validation_failure",
    "validation_failure",
    "LedgerFormatter",
    "BeancountLedgerFormatter",
    "MutationOperation",
    "MutationPlan",
    "MutationPlanner",
    "SidecarMutationStore",
    "SidecarSnapshot",
    "FilesystemSidecarMutationStore",
    "potential_write_targets",
    "sealed_write_set_matches",
    "MutationPreparationService",
    "MutationCommitMarker",
    "PublicationReconciliation",
    "PublishReceipt",
    "PublishRequest",
    "ReconciledRepositoryPublisher",
    "RepositoryPublisher",
]
