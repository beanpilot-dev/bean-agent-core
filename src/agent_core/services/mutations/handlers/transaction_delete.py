"""Transaction-delete lookup, policy, and plan construction."""

from typing import Any

from ...ledger_paths import is_sidecar_path
from ...transaction_index import TransactionIndex, parse_transaction_ref
from ...types import InvariantViolation, LedgerConfig
from ..facts import capture_transaction_revision_fact
from ..planners import MutationPlanner
from .contracts import PreparedMutation


class TransactionDeletePreparationHandler:
    """Prepare one exact sidecar transaction deletion for elevated review."""

    handler_key = "delete_transaction"

    def build(
        self,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: Any,
    ) -> PreparedMutation | InvariantViolation:
        transaction_ref = str(kwargs["transaction_ref"])
        revision_fingerprint = str(kwargs["revision_fingerprint"])
        commit_message = str(kwargs["commit_message"])

        if parse_transaction_ref(transaction_ref) is None:
            return InvariantViolation(
                invariant="MALFORMED_TRANSACTION_REF",
                severity="HARD",
                provided={"transaction_ref": transaction_ref},
                remediation=(
                    "Use ledger_find_transactions, then pass its unchanged transaction_ref "
                    "to ledger_get_transaction before preparing a deletion."
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
                    "Only transactions in the agent-managed sidecar can be deleted. "
                    "Record a correcting sidecar transaction instead."
                ),
            )

        plan = MutationPlanner.transaction_delete(
            transaction.relative_path,
            transaction.directive,
            transaction.start_line,
            commit_message,
        ).with_semantic_facts(
            (capture_transaction_revision_fact(workspace, transaction_ref, ledger_config),)
        )
        deletion_summary = "Remove exactly this agent-managed transaction from the sidecar."
        return PreparedMutation(
            handler_key=self.handler_key,
            action_type="delete_transaction",
            plan=plan,
            preview_fields={
                "transaction_ref": transaction_ref,
                "revision_fingerprint": revision_fingerprint,
                "old_directive": transaction.directive,
                "removed_directive": transaction.directive,
                "source_path": transaction.relative_path,
                "source_start_line": transaction.start_line,
                "source_end_line": transaction.end_line,
                "commit_message": commit_message,
                "deletion_summary": deletion_summary,
                "deletion_classification": "high_risk_transaction_deletion",
            },
            execution_spec={
                "transaction_ref": transaction_ref,
                "revision_fingerprint": revision_fingerprint,
                "commit_message": commit_message,
            },
            display_fields={
                "kind": "transaction_delete_preview",
                "title": "Delete transaction",
                "summary": deletion_summary,
                "transaction_ref": transaction_ref,
                "revision_fingerprint": revision_fingerprint,
                "source_path": transaction.relative_path,
                "source_start_line": transaction.start_line,
                "source_end_line": transaction.end_line,
                "removed_directive": transaction.directive,
                "deletion_classification": "high_risk_transaction_deletion",
                "risk": "high",
                "diff": _deletion_diff(transaction.relative_path, transaction.directive),
            },
            validation_fields={
                "file": transaction.relative_path,
                "transaction_ref": transaction_ref,
                "revision_fingerprint": revision_fingerprint,
                "deletion_classification": "high_risk_transaction_deletion",
            },
            message=(
                "Deletion passed dry-run validation. This high-risk action requires "
                "explicit approval."
            ),
        )


def _deletion_diff(path: str, directive: str) -> str:
    lines = directive.rstrip("\n").splitlines() or [directive]
    return "\n".join(
        [
            f"--- {path}",
            "+++ /dev/null",
            f"@@ -1,{len(lines)} +0,0 @@",
            *[f"-{line}" for line in lines],
        ]
    )
