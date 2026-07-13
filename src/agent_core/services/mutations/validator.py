"""Isolated mutation-plan validation with no repository publishing capability."""

import os
import tempfile
from dataclasses import dataclass
from typing import Protocol

from ..beancount import Beancount
from ..types import LedgerConfig, ValidationSummary
from . import sidecar
from .applier import MutationApplier
from .plans import MutationPlan


class LedgerValidator(Protocol):
    """Narrow validation dependency used by mutation dry-runs."""

    def check(
        self, workspace: str, config: LedgerConfig | None = None
    ) -> tuple[bool, str]: ...


class BeancountLedgerValidator:
    """Production adapter for the narrow ledger validation port."""

    def check(
        self, workspace: str, config: LedgerConfig | None = None
    ) -> tuple[bool, str]:
        return Beancount.bean_check(workspace, config)


@dataclass(frozen=True)
class PlanValidation:
    touched_files: tuple[str, ...]
    validation: ValidationSummary
    check_output: str = ""


class MutationValidator:
    """Apply and validate plans in a disposable workspace.

    It intentionally has neither a publisher constructor argument nor a
    publishing method: dry-run validation cannot commit or push Git changes.
    """

    def __init__(
        self,
        applier: MutationApplier | None = None,
        ledger_validator: LedgerValidator | None = None,
    ) -> None:
        self._applier = applier or MutationApplier()
        self._ledger_validator = ledger_validator or BeancountLedgerValidator()

    def validate(
        self, workspace: str, plan: MutationPlan, config: LedgerConfig | None = None
    ) -> PlanValidation:
        with tempfile.TemporaryDirectory(prefix="beanpilot-dry-run-") as tmp:
            dry_workspace = os.path.join(tmp, "workspace")
            sidecar.copy_workspace(workspace, dry_workspace)
            try:
                touched = self._applier.apply(dry_workspace, plan, config)
                clean, output = self._ledger_validator.check(dry_workspace, config)
                validation = (
                    ValidationSummary(status="validated", isolated=True)
                    if clean
                    else ValidationSummary(status="failed")
                )
                return PlanValidation(touched, validation, output)
            finally:
                Beancount.invalidate_workspace(dry_workspace)
