"""Compatibility façade over the shared parser-backed transaction index."""

from dataclasses import dataclass

from .transaction_index import TransactionIndex
from .types import LedgerConfig


@dataclass(frozen=True)
class LocatedTransaction:
    """One exact transaction block and its source path.

    ``file_content`` remains as a compatibility field for old callers but is
    intentionally empty: no locator path may expose a complete source file.
    """

    relative_path: str
    file_content: str
    block: str
    transaction_ref: str = ""


class TransactionLocator:
    """Legacy date/narration lookup backed by the authoritative transaction index."""

    @staticmethod
    def find(
        workspace: str,
        target_date: str,
        narration: str,
        ledger_config: LedgerConfig | None = None,
    ) -> list[LocatedTransaction]:
        index = TransactionIndex.build(workspace, ledger_config)
        matches = index.search(narration_contains=narration)
        return [
            LocatedTransaction(
                relative_path=match.relative_path,
                file_content="",
                block=match.directive.rstrip("\r\n"),
                transaction_ref=match.transaction_ref,
            )
            for match in matches
            if match.facts["date"] == target_date
        ]
