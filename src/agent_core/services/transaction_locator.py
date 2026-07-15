"""Read-only location of raw transactions in the configured ledger workspace."""

import os
import re
from dataclasses import dataclass

from .beancount import Beancount
from .types import LedgerConfig


@dataclass(frozen=True)
class LocatedTransaction:
    """One raw transaction block and its source file."""

    relative_path: str
    file_content: str
    block: str


class TransactionLocator:
    """Find raw transaction blocks without mutating the workspace."""

    @staticmethod
    def find(
        workspace: str,
        target_date: str,
        narration: str,
        ledger_config: LedgerConfig | None = None,
    ) -> list[LocatedTransaction]:
        escaped_narration = narration.replace('"', '\\"')
        bql = (
            "SELECT DISTINCT date, narration "
            f'WHERE date = {target_date} AND narration ~ "{escaped_narration}"'
        )
        rows, error = Beancount.run_bql_rows(workspace, bql, ledger_config)
        if error or not rows:
            return []

        header_re = re.compile(
            rf"^{re.escape(target_date)}\s+[*!].*?{re.escape(narration)}",
            re.MULTILINE,
        )
        results: list[LocatedTransaction] = []
        try:
            for dirpath, dirnames, filenames in os.walk(workspace):
                dirnames[:] = [name for name in dirnames if name not in {".git", ".venv"}]
                for filename in sorted(filenames):
                    if not filename.endswith(".beancount"):
                        continue
                    absolute_path = os.path.join(dirpath, filename)
                    relative_path = os.path.relpath(absolute_path, workspace)
                    try:
                        with open(absolute_path, encoding="utf-8") as handle:
                            content = handle.read()
                    except OSError:
                        continue
                    for match in header_re.finditer(content):
                        rest = content[match.start() :]
                        end_match = re.search(r"\n[ \t]*\n", rest)
                        block = (
                            rest[: end_match.start()].rstrip()
                            if end_match
                            else rest.rstrip()
                        )
                        results.append(
                            LocatedTransaction(relative_path, content, block)
                        )
        except OSError:
            pass
        return results
