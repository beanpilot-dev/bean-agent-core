"""Isolated mutation-plan validation with no repository publishing capability."""

import os
import tempfile
from dataclasses import asdict, dataclass
from typing import Protocol

from ..beancount import Beancount, LedgerServiceError
from ..types import LedgerConfig, ValidationFailed, ValidationSummary
from .applier import MutationApplier
from .persistence import SidecarMutationStore
from .plans import MutationPlan
from .sidecar import FilesystemSidecarMutationStore


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


def summarize_validation_failure(output: str) -> ValidationSummary:
    """Map raw deterministic validation output to the stable advisory contract."""
    lower = output.lower()
    error_type = "beancount_validation_error"
    if "does not balance" in lower:
        error_type = "transaction_not_balanced"
        messages = [
            "One or more transactions do not balance.",
            "Check posting signs, commodities, and whether one posting should be inferred.",
        ]
    elif "syntax error" in lower or "parser" in lower:
        error_type = "syntax_error"
        messages = [
            "The draft contains Beancount syntax that could not be parsed.",
            "Check dates, quotes, indentation, directives, and posting lines.",
        ]
    elif "balance failed" in lower or "balance assertion" in lower:
        error_type = "balance_assertion_failed"
        messages = [
            "The draft changes ledger balances in a way that violates an assertion.",
            "Review whether the proposed mutation belongs in the sidecar ledger.",
        ]
    else:
        messages = [
            "The draft does not pass deterministic Beancount validation.",
            "Revise the proposed ledger text and run the mutation tool again.",
        ]
    return ValidationSummary(
        status="failed",
        error_type=error_type,
        error_count=max(1, len([line for line in output.splitlines() if line.strip()])),
        messages=messages,
        retryable=True,
    )


def validation_failure(output: str, remediation: str) -> ValidationFailed:
    """Build the public validation failure returned by prepare and apply."""
    summary = summarize_validation_failure(output)
    return ValidationFailed(
        error=summary.error_type or "beancount_validation_error",
        remediation=remediation,
        advisory=asdict(summary),
    )


@dataclass(frozen=True)
class PlanValidation:
    touched_files: tuple[str, ...]
    validation: ValidationSummary
    check_output: str = ""
    failure: ValidationFailed | None = None


class MutationValidator:
    """Apply and validate plans in a disposable workspace.

    It intentionally has neither a publisher constructor argument nor a
    publishing method: dry-run validation cannot commit or push Git changes.
    """

    def __init__(
        self,
        applier: MutationApplier | None = None,
        ledger_validator: LedgerValidator | None = None,
        store: SidecarMutationStore | None = None,
    ) -> None:
        self._store = store or FilesystemSidecarMutationStore()
        self._applier = applier or MutationApplier(self._store)
        self._ledger_validator = ledger_validator or BeancountLedgerValidator()

    def validate(
        self, workspace: str, plan: MutationPlan, config: LedgerConfig | None = None
    ) -> PlanValidation:
        try:
            with tempfile.TemporaryDirectory(prefix="beanpilot-dry-run-") as tmp:
                dry_workspace = os.path.join(tmp, "workspace")
                self._store.copy_workspace(workspace, dry_workspace)
                try:
                    touched = self._applier.apply(dry_workspace, plan, config)
                    clean, output = self._ledger_validator.check(dry_workspace, config)
                    if clean:
                        return PlanValidation(
                            touched,
                            ValidationSummary(status="validated", isolated=True),
                        )
                    summary = summarize_validation_failure(output)
                    return PlanValidation(
                        touched,
                        summary,
                        output,
                        validation_failure(output, plan.remediation),
                    )
                finally:
                    Beancount.invalidate_workspace(dry_workspace)
        except OSError as exc:
            raise LedgerServiceError("Dry-run validation workspace unavailable") from exc
