"""Bulk-commit input policy and plan construction."""

import re
from typing import Any

from ...types import InvariantViolation, LedgerConfig
from ..facts import capture_account_state_fact
from ..planners import MutationPlanner
from .contracts import PreparedMutation
from .transaction_commit import extract_posting_accounts, validate_posting_accounts


class BulkCommitPreparationHandler:
    handler_key = "bulk_commit"

    def build(
        self,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: Any,
    ) -> PreparedMutation | InvariantViolation:
        transactions_text = str(kwargs.get("transactions_text") or "")
        commit_message = str(kwargs.get("commit_message") or "")
        transactions_file = kwargs.get("transactions_file")
        whitelist = kwargs.get("whitelist")
        if whitelist is not None and not isinstance(whitelist, list):
            whitelist = None
        if isinstance(transactions_file, str) and transactions_file:
            try:
                with open(transactions_file, encoding="utf-8") as handle:
                    transactions_text = handle.read()
            except OSError as exc:
                return InvariantViolation(
                    invariant="STAGING_ERROR",
                    severity="HARD",
                    provided=transactions_file,
                    remediation=f"Cannot read staging file: {exc}",
                )
        elif not transactions_text:
            return InvariantViolation(
                invariant="MISSING_INPUT",
                severity="HARD",
                provided=None,
                remediation="Provide transactions_text or transactions_file.",
            )
        violation = validate_posting_accounts(
            workspace, transactions_text, whitelist, ledger_config
        )
        if violation:
            return violation
        accounts = extract_posting_accounts(transactions_text)
        plan = MutationPlanner.bulk(transactions_text, commit_message).with_semantic_facts(
            tuple(
                capture_account_state_fact(workspace, account, ledger_config)
                for account in accounts
            )
        )
        transaction_lines = [
            line
            for line in transactions_text.splitlines()
            if re.match(r"^\d{4}-\d{2}-\d{2}\s+[*!]", line)
        ]
        transaction_count = len(transaction_lines)
        sample = "\n".join(transaction_lines[:5])
        if transaction_count > 5:
            sample += f"\n... ({transaction_count - 5} more)"
        return PreparedMutation(
            handler_key=self.handler_key,
            action_type="bulk_commit",
            plan=plan,
            preview_fields={
                "transaction_count": transaction_count,
                "sample": sample,
                "commit_message": commit_message,
            },
            execution_spec={
                "transactions_text": transactions_text,
                "commit_message": commit_message,
            },
            display_fields={
                "kind": "bulk_import_preview",
                "summary": "Record multiple transactions",
                "diff": sample,
            },
            validation_fields={"transaction_count": transaction_count},
            validation_preview_fields=("target_file",),
            message=f"{transaction_count} transactions passed dry-run validation.",
            preview_target_field="target_file",
        )
