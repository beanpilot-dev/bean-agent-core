"""Explicit registry for canonical mutation preparation handlers."""

from .account_close import AccountClosePreparationHandler
from .account_open import AccountOpenPreparationHandler
from .balance_reconciliation import BalanceReconciliationPreparationHandler
from .balance_update import BalanceUpdatePreparationHandler
from .bulk_commit import BulkCommitPreparationHandler
from .change_set import ChangeSetPreparationHandler
from .contracts import MutationPreparationHandler
from .price import PricePreparationHandler
from .transaction_commit import TransactionCommitPreparationHandler
from .transaction_delete import TransactionDeletePreparationHandler
from .transaction_update import TransactionUpdatePreparationHandler


class MutationPreparationHandlerRegistry:
    """Resolve preparation intent keys without action branches in the shared shell."""

    def __init__(self, handlers: tuple[MutationPreparationHandler, ...] | None = None) -> None:
        registered = handlers or (
            TransactionCommitPreparationHandler(),
            AccountOpenPreparationHandler(),
            AccountClosePreparationHandler(),
            TransactionUpdatePreparationHandler(),
            TransactionDeletePreparationHandler(),
            PricePreparationHandler(),
            BulkCommitPreparationHandler(),
            ChangeSetPreparationHandler(),
            BalanceReconciliationPreparationHandler(),
            BalanceUpdatePreparationHandler(),
        )
        self._handlers = {handler.handler_key: handler for handler in registered}

    def get(self, handler_key: str) -> MutationPreparationHandler:
        return self._handlers[handler_key]

    def keys(self) -> tuple[str, ...]:
        return tuple(self._handlers)
