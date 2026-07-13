"""Compatibility facade for deterministic ledger mutation operations.

New code composes the focused mutation package.  This module intentionally
retains the historic ``LedgerService`` entry point while delegating all
mutation preparation and legacy direct-confirm compatibility to
``MutationPreparationService``.
"""

from .mutations.application import PendingActionApplicationService
from .mutations.preparation import (
    Beancount,
    LedgerServiceError,
    MutationPreparationService,
    _git_dependency_error,
)

__all__ = ["Beancount", "LedgerService", "LedgerServiceError", "_git_dependency_error"]


class LedgerService:
    """Backward-compatible facade for existing tools and API callers.

    The public methods are deliberately resolved from one preparation service
    instance.  This keeps the historic construction and method signatures
    while preventing callers from depending on the extracted implementation.
    """

    def __init__(self) -> None:
        self._preparation = MutationPreparationService()
        self._application = PendingActionApplicationService()

    def __getattr__(self, name: str):
        return getattr(self._preparation, name)

    def apply_pending_action(self, *args, **kwargs):
        """Apply a sealed plan, or dispatch a characterized legacy action."""
        return self._application.apply_pending_action(self._preparation, *args, **kwargs)

    # Existing callers use these inspection helpers directly on the class.
    _extract_accounts = staticmethod(MutationPreparationService._extract_accounts)
    validate_accounts = staticmethod(MutationPreparationService.validate_accounts)
    find_transaction_block = staticmethod(MutationPreparationService.find_transaction_block)
    preflight_report = staticmethod(MutationPreparationService.preflight_report)
