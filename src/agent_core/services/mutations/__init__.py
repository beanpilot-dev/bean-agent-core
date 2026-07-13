"""Shared deterministic ledger mutation planning and execution primitives.

This package deliberately has no dependency on the workflow or pending-action
layers.  Callers construct plans from approved inputs and use the coordinator
for both dry-run validation and durable application.
"""

from .coordinator import MutationCoordinator
from .planners import MutationPlanner
from .plans import MutationOperation, MutationPlan

__all__ = ["MutationCoordinator", "MutationOperation", "MutationPlan", "MutationPlanner"]
