"""Transaction-update lookup, policy, and plan construction."""

import re
from typing import Any

from ...ledger_paths import is_sidecar_path
from ...transaction_index import TransactionIndex, parse_transaction_ref
from ...types import InvariantViolation, LedgerConfig
from ..facts import capture_account_state_fact, capture_transaction_revision_fact
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

    def build(
        self,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: Any,
    ) -> PreparedMutation | InvariantViolation:
        transaction_ref = str(kwargs["transaction_ref"])
        revision_fingerprint = str(kwargs["revision_fingerprint"])
        new_transaction_text = str(kwargs["new_transaction_text"])
        commit_message = str(kwargs["commit_message"])
        whitelist = kwargs.get("whitelist")
        if whitelist is not None and not isinstance(whitelist, list):
            whitelist = None

        if parse_transaction_ref(transaction_ref) is None:
            return InvariantViolation(
                invariant="MALFORMED_TRANSACTION_REF",
                severity="HARD",
                provided={"transaction_ref": transaction_ref},
                remediation=(
                    "Use ledger_find_transactions, then pass its unchanged transaction_ref "
                    "to ledger_get_transaction before preparing an update."
                ),
            )

        try:
            index = TransactionIndex.build(workspace, ledger_config)
        except Exception:
            return InvariantViolation(
                invariant="LEDGER_PARSE_ERROR",
                severity="HARD",
                provided={"transaction_ref": transaction_ref},
                remediation="Refresh the ledger and repeat the authoritative lookup.",
            )

        resolution, transaction = index.resolve(transaction_ref)
        if transaction is None:
            invariant = {
                "MALFORMED_TRANSACTION_REF": "MALFORMED_TRANSACTION_REF",
                "TRANSACTION_NOT_FOUND": "TRANSACTION_NOT_FOUND",
                "STALE_TRANSACTION_REF": "STALE_TRANSACTION_REF",
                "AMBIGUOUS_TRANSACTION_REF": "AMBIGUOUS_TRANSACTION_REF",
            }.get(resolution, "TRANSACTION_NOT_FOUND")
            return InvariantViolation(
                invariant=invariant,
                severity="HARD",
                provided={"transaction_ref": transaction_ref},
                remediation=(
                    "The transaction reference is no longer authoritative. Use "
                    "ledger_find_transactions, then ledger_get_transaction again."
                ),
            )
        if revision_fingerprint != transaction.revision_fingerprint:
            return InvariantViolation(
                invariant="STALE_TRANSACTION_REVISION",
                severity="HARD",
                provided={
                    "transaction_ref": transaction_ref,
                    "revision_fingerprint": revision_fingerprint,
                },
                remediation=(
                    "The transaction changed after it was read. Use ledger_get_transaction "
                    "again and pass its current revision_fingerprint."
                ),
                detail={"current_revision_fingerprint": transaction.revision_fingerprint},
            )

        if not is_sidecar_path(transaction.relative_path, ledger_config):
            return InvariantViolation(
                invariant="SIDECAR_WRITE_ISOLATION",
                severity="HARD",
                provided={"file": transaction.relative_path},
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
        advisory = detect_value_change(transaction.directive, new_transaction_text)
        accounts = extract_posting_accounts(new_transaction_text)
        plan = MutationPlanner.transaction_update(
            transaction.relative_path,
            transaction.directive,
            new_transaction_text,
            commit_message,
        ).with_semantic_facts(
            (
                capture_transaction_revision_fact(
                    workspace, transaction_ref, ledger_config
                ),
                *(
                    capture_account_state_fact(workspace, account, ledger_config)
                    for account in accounts
                ),
            )
        )
        return PreparedMutation(
            handler_key=self.handler_key,
            action_type="update_transaction",
            plan=plan,
            preview_fields={
                "transaction_ref": transaction_ref,
                "revision_fingerprint": revision_fingerprint,
                "old_directive": transaction.directive,
                "new_directive": new_transaction_text.strip(),
                "source_path": transaction.relative_path,
                "source_start_line": transaction.start_line,
                "source_end_line": transaction.end_line,
                "commit_message": commit_message,
                "advisory": advisory,
            },
            execution_spec={
                "transaction_ref": transaction_ref,
                "revision_fingerprint": revision_fingerprint,
                "new_transaction_text": new_transaction_text,
                "commit_message": commit_message,
            },
            display_fields={
                "kind": "transaction_update_preview",
                "summary": "Update a transaction",
                "transaction_ref": transaction_ref,
                "revision_fingerprint": revision_fingerprint,
                "source_path": transaction.relative_path,
                "source_start_line": transaction.start_line,
                "source_end_line": transaction.end_line,
                "old_directive": transaction.directive,
                "new_directive": new_transaction_text.strip(),
                "diff": new_transaction_text.strip(),
            },
            validation_fields={
                "file": transaction.relative_path,
                "advisory": advisory,
                "transaction_ref": transaction_ref,
                "revision_fingerprint": revision_fingerprint,
            },
            message="Replacement passed dry-run validation. Request explicit approval.",
        )
