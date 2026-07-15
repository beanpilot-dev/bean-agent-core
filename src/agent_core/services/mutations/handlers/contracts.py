"""Data contracts shared by mutation preparation handlers."""

from dataclasses import dataclass, field
from typing import Any, Protocol

from ...types import InvariantViolation, LedgerConfig, ValidationFailed
from ..plans import MutationPlan


@dataclass(frozen=True)
class PreparedMutation:
    """Action-owned facts awaiting shared validation and approval materialization."""

    handler_key: str
    action_type: str
    plan: MutationPlan
    preview_fields: dict[str, object]
    execution_spec: dict[str, object]
    display_fields: dict[str, object]
    validation_fields: dict[str, object]
    message: str
    preview_target_field: str | None = None
    validation_preview_fields: tuple[str, ...] = field(default_factory=tuple)
    embed_preview_in_display: bool = True


class MutationPreparationHandler(Protocol):
    """Build one action's plan and presentation facts from a read-only workspace."""

    handler_key: str

    def build(
        self, workspace: str, ledger_config: LedgerConfig | None = None, **kwargs: Any
    ) -> PreparedMutation | InvariantViolation | ValidationFailed: ...
