"""Transaction-update lookup, policy, and plan construction."""

import re
from typing import Any

from ...ledger_paths import is_sidecar_path
from ...transaction_locator import TransactionLocator
from ...types import InvariantViolation, LedgerConfig
from ..facts import capture_account_state_fact
from ..planners import MutationPlanner
from .contracts import PreparedMutation
from .transaction_commit import extract_posting_accounts, validate_posting_accounts

_AMOUNT_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*\s+[A-Z][A-Z0-9\-]+")


def detect_value_change(old_text: str, new_text: str) -> dict[str, object] | None:
    """Describe balance-affecting amount or account changes."""
    old_amounts = set(_AMOUNT_RE.findall(old_text))
    new_amounts = set(_AMOUNT_RE.findall(new_text))
    old_accounts = set(extract_posting_accounts(old_text))
    new_accounts = set(extract_posting_accounts(new_text))
    changes: dict[str, object] = {}
    if old_amounts != new_amounts:
        changes["amounts"] = {
            "removed": sorted(old_amounts - new_amounts),
            "added": sorted(new_amounts - old_amounts),
        }
    if old_accounts != new_accounts:
        changes["accounts"] = {
            "removed": sorted(old_accounts - new_accounts),
            "added": sorted(new_accounts - old_accounts),
        }
    if not changes:
        return None
    return {
        "severity": "ADVISORY",
        "warning": "VALUE_CHANGED",
        "changes": changes,
        "note": (
            "Amount or account changes shift running balances. "
            "If balance assertions exist, bean-check may fail."
        ),
    }


class TransactionUpdatePreparationHandler:
    handler_key = "update_transaction"

    def __init__(self, locator: TransactionLocator | None = None) -> None:
        self._locator = locator or TransactionLocator()

    def build(
        self,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: Any,
    ) -> PreparedMutation | InvariantViolation:
        target_date = str(kwargs["target_date"])
        narration = str(kwargs["narration"])
        new_transaction_text = str(kwargs["new_transaction_text"])
        commit_message = str(kwargs["commit_message"])
        whitelist = kwargs.get("whitelist")
        if whitelist is not None and not isinstance(whitelist, list):
            whitelist = None
        matches = self._locator.find(workspace, target_date, narration, ledger_config)
        if not matches:
            return InvariantViolation(
                invariant="TRANSACTION_NOT_FOUND",
                severity="HARD",
                provided={"date": target_date, "narration": narration},
                remediation=(
                    "No transaction found. Use find_transactions to locate the exact entry."
                ),
            )
        if len(matches) > 1:
            return InvariantViolation(
                invariant="AMBIGUOUS_MATCH",
                severity="HARD",
                provided={"date": target_date, "narration": narration},
                remediation="Provide a more specific narration substring.",
                detail={
                    "matches_found": [
                        {"file": match.relative_path, "block": match.block}
                        for match in matches
                    ]
                },
            )
        located = matches[0]
        if not is_sidecar_path(located.relative_path, ledger_config):
            return InvariantViolation(
                invariant="SIDECAR_WRITE_ISOLATION",
                severity="HARD",
                provided={"file": located.relative_path},
                remediation=(
                    "Only transactions in the agent-managed sidecar can be updated. "
                    "Record a correcting sidecar transaction instead."
                ),
            )
        violation = validate_posting_accounts(
            workspace, new_transaction_text, whitelist, ledger_config
        )
        if violation:
            return violation
        advisory = detect_value_change(located.block, new_transaction_text)
        accounts = extract_posting_accounts(new_transaction_text)
        plan = MutationPlanner.update(
            located.relative_path,
            located.block,
            new_transaction_text,
            commit_message,
        ).with_semantic_facts(
            tuple(
                capture_account_state_fact(workspace, account, ledger_config)
                for account in accounts
            )
        )
        return PreparedMutation(
            handler_key=self.handler_key,
            action_type="update_transaction",
            plan=plan,
            preview_fields={
                "found_block": located.block,
                "replacement": new_transaction_text.strip(),
                "file": located.relative_path,
                "commit_message": commit_message,
                "advisory": advisory,
            },
            execution_spec={
                "target_date": target_date,
                "narration": narration,
                "new_transaction_text": new_transaction_text,
                "commit_message": commit_message,
            },
            display_fields={
                "kind": "transaction_update_preview",
                "summary": "Update a transaction",
                "diff": new_transaction_text,
            },
            validation_fields={"file": located.relative_path, "advisory": advisory},
            message="Replacement passed dry-run validation. Request explicit approval.",
        )
