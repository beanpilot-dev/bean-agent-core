"""Action-specific construction of immutable ledger mutation plans.

This module deliberately stops at planning.  It does not inspect workspaces,
create pending actions, validate Beancount, or publish Git commits; those
responsibilities stay with the caller's read, approval, and coordinator ports.
"""

from .plans import MutationOperation, MutationPlan


class MutationPlanner:
    """Build the canonical operation sequence for each supported mutation."""

    @staticmethod
    def commit(transaction_text: str, commit_message: str) -> MutationPlan:
        return MutationPlan.from_operations(
            [MutationOperation(kind="append", text=transaction_text)],
            commit_message=commit_message,
            remediation="Fix the transaction syntax or balancing and prepare it again.",
        )

    @staticmethod
    def open_account(
        account_name: str, directive_text: str, commit_message: str | None = None
    ) -> MutationPlan:
        return MutationPlan.from_operations(
            [MutationOperation(kind="open", account_name=account_name, text=directive_text)],
            commit_message=commit_message or f"chore(accounts): open {account_name}",
            remediation="Fix the account directive and prepare it again.",
        )

    @staticmethod
    def transaction_update(
        target_file: str, old_text: str, new_text: str, commit_message: str
    ) -> MutationPlan:
        return MutationPlan.from_operations(
            [
                MutationOperation(
                    kind="replace",
                    target_file=target_file,
                    old_text=old_text,
                    text=new_text,
                )
            ],
            commit_message=commit_message,
            remediation=(
                "bean-check failed after replacement. Adjust the transaction and prepare it again."
            ),
        )

    @staticmethod
    def transaction_delete(
        target_file: str,
        old_text: str,
        target_start_line: int,
        commit_message: str,
    ) -> MutationPlan:
        return MutationPlan.from_operations(
            [
                MutationOperation(
                    kind="delete",
                    target_file=target_file,
                    old_text=old_text,
                    target_start_line=target_start_line,
                )
            ],
            commit_message=commit_message,
            remediation=(
                "The transaction changed before deletion could be applied. "
                "Look it up again and prepare a new deletion."
            ),
        )

    @staticmethod
    def bulk(transactions_text: str, commit_message: str) -> MutationPlan:
        return MutationPlan.from_operations(
            [MutationOperation(kind="append", text=transactions_text)],
            commit_message=commit_message,
            remediation="bean-check failed. Revise the transaction batch and prepare it again.",
        )

    @staticmethod
    def change_set(
        operations: list[MutationOperation], commit_message: str
    ) -> MutationPlan:
        return MutationPlan.from_operations(
            operations,
            commit_message=commit_message,
            remediation="Fix the change-set operations and prepare them again.",
        )

    @staticmethod
    def reconciliation(
        generated_text: str, commit_message: str, *, checkpoint_update: bool = False
    ) -> MutationPlan:
        action = "update balance checkpoint" if checkpoint_update else "reconcile balance"
        return MutationPlan.from_operations(
            [MutationOperation(kind="append", text=generated_text)],
            commit_message=commit_message or f"chore(ledger): {action}",
            remediation="Fix the reconciliation inputs and prepare it again.",
        )
