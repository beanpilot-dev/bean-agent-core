"""LedgerService — deterministic Beancount read/write business logic.

Pure Python business logic with no LLM, no ContextVar dependencies.
All methods accept workspace and other parameters explicitly.

Write operations use a preview→confirm split:
  - preview_*  → validates and returns Preview (proposal_id is informational)
  - confirm_*  → accepts full payload, calls preview_* internally for re-validation,
                  then executes and returns CommitResult

The LLM carries the full payload both times; agent-core is stateless so
proposal_id cannot survive across requests. confirm_* always re-validates.

Domain errors (invalid account, bad syntax) return InvariantViolation /
ValidationFailed for the Tool Layer to route back to the LLM.
System errors (disk full, git failure) raise exceptions.
"""

import logging
import os
import re
import uuid
from datetime import date

from .types import (
    CommitResult,
    DependencyUnavailable,
    InvariantViolation,
    PreflightResult,
    Preview,
    QueryResult,
    ValidationFailed,
)
from .workspace import GitService

logger = logging.getLogger(__name__)


class LedgerServiceError(Exception):
    """Unrecoverable ledger operation failure."""


# ---------------------------------------------------------------------------
# Beancount CLI helpers (lightweight local copy)
# ---------------------------------------------------------------------------

class Beancount:

    @staticmethod
    def _bean_bin(workspace: str, name: str) -> str:
        import sys
        candidate = os.path.join(workspace, ".venv", "bin", name)
        if os.path.exists(candidate):
            return candidate
        candidate = os.path.join(os.path.dirname(sys.executable), name)
        if os.path.exists(candidate):
            return candidate
        return name

    @staticmethod
    def bean_check(workspace: str) -> tuple[bool, str]:
        import subprocess
        main = os.path.join(workspace, "data", "main.beancount")
        result = subprocess.run(
            [Beancount._bean_bin(workspace, "bean-check"), main],
            cwd=workspace, capture_output=True, text=True,
        )
        return result.returncode == 0, result.stdout + result.stderr

    @staticmethod
    def bean_format(workspace: str, file_path: str) -> None:
        import subprocess
        result = subprocess.run(
            [Beancount._bean_bin(workspace, "bean-format"), file_path],
            cwd=workspace, capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout:
            with open(file_path, "w") as f:
                f.write(result.stdout)
        elif result.returncode != 0:
            logger.warning(
                "bean-format failed on %s: %s", file_path, result.stderr.strip()
            )

    @staticmethod
    def run_bql_rows(workspace: str, bql: str) -> tuple[list[dict], str | None]:
        import csv
        import io
        import subprocess
        main = os.path.join(workspace, "data", "main.beancount")
        result = subprocess.run(
            [Beancount._bean_bin(workspace, "bean-query"), "-f", "csv", main, bql],
            cwd=workspace, capture_output=True, text=True,
        )
        if result.returncode != 0:
            return [], result.stderr.strip()
        rows = list(csv.DictReader(io.StringIO(result.stdout)))
        return [{k: v.strip() for k, v in row.items()} for row in rows], None


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_ACCOUNT_NAME_RE = re.compile(
    r"^(Assets|Liabilities|Equity|Income|Expenses)(:[A-Z][A-Za-z0-9\-]+)+$"
)
_POSTING_ACCOUNT_RE = re.compile(
    r"^\s+(Assets|Liabilities|Equity|Income|Expenses)(?::[A-Za-z][A-Za-z0-9\-]+)+",
    re.MULTILINE,
)
_AMOUNT_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*\s+[A-Z][A-Z0-9\-]+")
_CHECK_INCLUDE = 'include "agent_inc/main.beancount"'


# ---------------------------------------------------------------------------
# Sidecar helpers
# ---------------------------------------------------------------------------

def _check_sidecar_include(workspace: str) -> bool:
    main = os.path.join(workspace, "data", "main.beancount")
    try:
        with open(main) as f:
            return _CHECK_INCLUDE in f.read()
    except OSError:
        return False


def _ensure_agent_sidecar(workspace: str) -> str:
    today = date.today()
    chunk_name = f"{today.year}-{today.month:02d}.beancount"
    agent_dir = os.path.join(workspace, "data", "agent_inc")
    os.makedirs(agent_dir, exist_ok=True)

    chunk_path = os.path.join(agent_dir, chunk_name)
    if not os.path.exists(chunk_path):
        with open(chunk_path, "w") as f:
            f.write(
                f"; Agent-generated transactions — {today.year}-{today.month:02d}\n"
            )

    agg_path = os.path.join(agent_dir, "main.beancount")
    include_line = f'include "{chunk_name}"\n'
    if os.path.exists(agg_path):
        with open(agg_path) as f:
            existing = f.read()
        if chunk_name not in existing:
            with open(agg_path, "a") as f:
                f.write(include_line)
    else:
        with open(agg_path, "w") as f:
            f.write("; Agent sidecar — auto-managed, do not edit manually\n")
            f.write(include_line)

    return f"data/agent_inc/{chunk_name}"


# ---------------------------------------------------------------------------
# LedgerService
# ---------------------------------------------------------------------------

class LedgerService:
    """Deterministic Beancount read/write operations.

    Write operations follow preview→confirm split:
      - preview_* returns Preview (validates, proposal_id is informational)
      - confirm_* accepts full payload, calls preview_* internally for re-validation,
        then executes directly

    All read operations are stateless (static).
    """

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_accounts(transaction_text: str) -> list[str]:
        return sorted({
            m.group(0).strip()
            for m in _POSTING_ACCOUNT_RE.finditer(transaction_text)
        })

    @staticmethod
    def get_accounts(workspace: str) -> list[str]:
        rows, err = Beancount.run_bql_rows(
            workspace, "SELECT DISTINCT account ORDER BY account"
        )
        if err:
            raise LedgerServiceError(f"Failed to list accounts: {err}")
        return [r["account"] for r in rows]

    @staticmethod
    def validate_accounts(
        workspace: str,
        transaction_text: str,
        whitelist: list[str] | None = None,
    ) -> InvariantViolation | None:
        used = LedgerService._extract_accounts(transaction_text)
        valid = set(LedgerService.get_accounts(workspace))

        unknown = [a for a in used if a not in valid]
        if unknown:
            return InvariantViolation(
                invariant="ACCOUNT_WHITELIST",
                severity="HARD",
                provided=unknown,
                remediation=(
                    "Unknown accounts detected. Use open_account "
                    "to create them first."
                ),
                detail={"valid_accounts": sorted(valid)},
            )

        if whitelist:
            out_of_scope = [
                a for a in used if not any(a.startswith(w) for w in whitelist)
            ]
            if out_of_scope:
                return InvariantViolation(
                    invariant="CONVERSATION_SCOPE",
                    severity="HARD",
                    provided=out_of_scope,
                    remediation=(
                        "These accounts are outside the current conversation scope. "
                        "Use accounts within the allowed prefixes."
                    ),
                    detail={"allowed_prefixes": whitelist},
                )

        return None

    # ═══════════════════════════════════════════════════════════════════════
    # commit_transaction → preview_commit + confirm_commit
    # ═══════════════════════════════════════════════════════════════════════

    def preview_commit(
        self,
        workspace: str,
        transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
    ) -> Preview | InvariantViolation:
        """Validate a transaction proposal. Returns Preview with proposal_id."""
        target = _ensure_agent_sidecar(workspace)

        violation = self.validate_accounts(workspace, transaction_text, whitelist)
        if violation:
            return violation

        accounts = LedgerService._extract_accounts(transaction_text)
        pid = f"prop_{uuid.uuid4().hex[:12]}"

        return Preview(
            proposal_id=pid,
            operation="commit_transaction",
            preview={
                "transaction": transaction_text,
                "accounts_validated": accounts,
                "target_file": target,
                "commit_message": commit_message,
            },
            message=(
                "All accounts validated. Show this preview to the user and "
                "call confirm_commit with the same transaction_text and "
                "commit_message after approval."
            ),
        )

    def confirm_commit(
        self,
        workspace: str,
        transaction_text: str,
        commit_message: str,
        github_token: str | None = None,
        whitelist: list[str] | None = None,
    ) -> CommitResult | ValidationFailed | DependencyUnavailable | InvariantViolation:
        """Validate and execute a transaction commit. Re-runs preview internally."""
        preview = self.preview_commit(workspace, transaction_text, commit_message, whitelist)
        if not isinstance(preview, Preview):
            return preview

        target = preview.preview["target_file"]
        target_path = os.path.join(workspace, target)
        backup_path = target_path + ".bak"

        with open(target_path) as f:
            original = f.read()
        with open(backup_path, "w") as f:
            f.write(original)

        with open(target_path, "a") as f:
            f.write(f"\n{transaction_text}\n")

        is_clean, check_output = Beancount.bean_check(workspace)
        if not is_clean:
            with open(target_path, "w") as f:
                f.write(original)
            os.remove(backup_path)
            return ValidationFailed(
                error=check_output.strip(),
                remediation="Fix the transaction syntax and try again.",
            )

        os.remove(backup_path)
        Beancount.bean_format(workspace, target_path)

        git = GitService.commit_and_push(workspace, commit_message, github_token)
        if not git["ok"]:
            return DependencyUnavailable(
                error=f"Written but git commit failed: {git['error']}",
            )

        return CommitResult(
            outcome="Transaction recorded, validated, and committed",
            result={
                "target_file": target,
                "commit_message": commit_message,
                "transaction": transaction_text,
            },
            push_status=git["push"],
        )

    # ═══════════════════════════════════════════════════════════════════════
    # open_account → preview_open + confirm_open
    # ═══════════════════════════════════════════════════════════════════════

    def preview_open(
        self,
        workspace: str,
        account_name: str,
        currency: str | None,
        open_date: str,
        display_name: str | None = None,
    ) -> Preview | InvariantViolation:
        """Validate an open-account proposal. Returns Preview with proposal_id."""
        if not _ACCOUNT_NAME_RE.match(account_name):
            return InvariantViolation(
                invariant="ACCOUNT_NAME_FORMAT",
                severity="HARD",
                provided=account_name,
                remediation=(
                    "Account names must follow Beancount format: "
                    "Type:Component (e.g. Assets:Liquid:Bank:NewAccount)."
                ),
            )

        existing = self.get_accounts(workspace)
        if account_name in existing:
            return InvariantViolation(
                invariant="ACCOUNT_ALREADY_EXISTS",
                severity="HARD",
                provided=account_name,
                remediation=f"Account '{account_name}' already exists.",
            )

        currency_part = f"  {currency}" if currency else ""
        directive_lines = [f"{open_date} open {account_name}{currency_part}"]
        if display_name:
            directive_lines.append(f'  name: "{display_name}"')
        directive_text = "\n".join(directive_lines)
        pid = f"prop_{uuid.uuid4().hex[:12]}"

        return Preview(
            proposal_id=pid,
            operation="open_account",
            preview={
                "directive": directive_text,
                "account": account_name,
                "currency": currency,
                "open_date": open_date,
            },
            message=(
                "Call confirm_open with the same account details after user approval."
            ),
        )

    def confirm_open(
        self,
        workspace: str,
        account_name: str,
        currency: str | None,
        open_date: str,
        display_name: str | None = None,
        github_token: str | None = None,
    ) -> CommitResult | ValidationFailed | DependencyUnavailable | InvariantViolation:
        """Validate and execute an open-account. Re-runs preview internally."""
        preview = self.preview_open(workspace, account_name, currency, open_date, display_name)
        if not isinstance(preview, Preview):
            return preview

        directive_text = preview.preview["directive"]
        directive_lines = directive_text.split("\n")

        _ensure_agent_sidecar(workspace)
        main_path = os.path.join(workspace, "data", "agent_inc", "main.beancount")

        try:
            with open(main_path) as f:
                original = f.read()
        except OSError as e:
            return DependencyUnavailable(error=f"Cannot read main.beancount: {e}")

        account_type = account_name.split(":")[0]
        lines = original.splitlines()
        insert_after = -1
        for i, line in enumerate(lines):
            if re.match(rf"^\d{{4}}-\d{{2}}-\d{{2}} open {account_type}", line):
                insert_after = i
        if insert_after == -1:
            for i, line in enumerate(lines):
                if re.match(r"^\d{4}-\d{2}-\d{2} open ", line):
                    insert_after = i

        if insert_after >= 0:
            for j, dl in enumerate(directive_lines):
                lines.insert(insert_after + 1 + j, dl)
            new_content = "\n".join(lines) + "\n"
        else:
            new_content = original.rstrip("\n") + "\n\n" + directive_text + "\n"

        with open(main_path, "w") as f:
            f.write(new_content)

        is_clean, check_output = Beancount.bean_check(workspace)
        if not is_clean:
            with open(main_path, "w") as f:
                f.write(original)
            return ValidationFailed(
                error=check_output.strip(),
                remediation="Fix the account directive and try again.",
            )

        Beancount.bean_format(workspace, main_path)
        git = GitService.commit_and_push(
            workspace, f"chore(accounts): open {account_name}", github_token,
        )
        if not git["ok"]:
            return DependencyUnavailable(
                error=f"Written but git commit failed: {git['error']}",
            )

        return CommitResult(
            outcome=f"Account '{account_name}' opened and committed",
            result={
                "account": account_name,
                "currency": currency,
                "open_date": open_date,
                "file": "data/agent_inc/main.beancount",
            },
            push_status=git["push"],
        )

    # ═══════════════════════════════════════════════════════════════════════
    # update_transaction → preview_update + confirm_update
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def find_transaction_block(
        workspace: str, target_date: str, narration: str,
    ) -> list[tuple[str, str, str]]:
        """Search beancount files for a transaction matching date + narration.

        Returns list of (rel_path, file_content, raw_block) tuples.
        """
        header_re = re.compile(
            rf"^{re.escape(target_date)}\s+[*!].*?{re.escape(narration)}",
            re.MULTILINE,
        )
        results: list[tuple[str, str, str]] = []
        data_dir = os.path.join(workspace, "data")

        try:
            for name in sorted(os.listdir(data_dir)):
                if re.match(r"^\d{4}\.beancount$", name):
                    abs_path = os.path.join(data_dir, name)
                    try:
                        with open(abs_path) as f:
                            content = f.read()
                    except OSError:
                        continue
                    for m in header_re.finditer(content):
                        block_start = m.start()
                        rest = content[block_start:]
                        end_match = re.search(r"\n[ \t]*\n", rest)
                        raw = (
                            rest[:end_match.start()].rstrip()
                            if end_match else rest.rstrip()
                        )
                        results.append((f"data/{name}", content, raw))
        except OSError:
            pass

        agent_dir = os.path.join(workspace, "data", "agent_inc")
        try:
            for name in sorted(os.listdir(agent_dir)):
                if re.match(r"^\d{4}-\d{2}\.beancount$", name):
                    abs_path = os.path.join(agent_dir, name)
                    try:
                        with open(abs_path) as f:
                            content = f.read()
                    except OSError:
                        continue
                    for m in header_re.finditer(content):
                        block_start = m.start()
                        rest = content[block_start:]
                        end_match = re.search(r"\n[ \t]*\n", rest)
                        raw = (
                            rest[:end_match.start()].rstrip()
                            if end_match else rest.rstrip()
                        )
                        results.append((f"data/agent_inc/{name}", content, raw))
        except OSError:
            pass

        return results

    @staticmethod
    def _detect_value_change(old_text: str, new_text: str) -> dict | None:
        old_amounts = set(_AMOUNT_RE.findall(old_text))
        new_amounts = set(_AMOUNT_RE.findall(new_text))
        old_accounts = {
            m.group(0).strip() for m in _POSTING_ACCOUNT_RE.finditer(old_text)
        }
        new_accounts = {
            m.group(0).strip() for m in _POSTING_ACCOUNT_RE.finditer(new_text)
        }
        changes: dict = {}
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

    def preview_update(
        self,
        workspace: str,
        target_date: str,
        narration: str,
        new_transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
    ) -> Preview | InvariantViolation:
        """Find and validate a replacement transaction. Returns Preview."""
        matches = self.find_transaction_block(workspace, target_date, narration)

        if not matches:
            return InvariantViolation(
                invariant="TRANSACTION_NOT_FOUND",
                severity="HARD",
                provided={"date": target_date, "narration": narration},
                remediation=(
                    "No transaction found. Use find_transactions to locate "
                    "the exact entry."
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
                        {"file": rel, "block": block}
                        for rel, _, block in matches
                    ],
                },
            )

        rel_path, _, old_block = matches[0]

        violation = self.validate_accounts(
            workspace, new_transaction_text, whitelist,
        )
        if violation:
            return violation

        advisory = self._detect_value_change(old_block, new_transaction_text)
        pid = f"prop_{uuid.uuid4().hex[:12]}"

        return Preview(
            proposal_id=pid,
            operation="update_transaction",
            preview={
                "found_block": old_block,
                "replacement": new_transaction_text.strip(),
                "file": rel_path,
                "commit_message": commit_message,
                "advisory": advisory,
            },
            message=(
                "Call confirm_update with the same parameters after user approval."
            ),
        )

    def confirm_update(
        self,
        workspace: str,
        target_date: str,
        narration: str,
        new_transaction_text: str,
        commit_message: str,
        github_token: str | None = None,
        whitelist: list[str] | None = None,
    ) -> CommitResult | ValidationFailed | DependencyUnavailable | InvariantViolation:
        """Validate and execute a transaction update. Re-runs preview internally."""
        preview = self.preview_update(
            workspace, target_date, narration, new_transaction_text,
            commit_message, whitelist,
        )
        if not isinstance(preview, Preview):
            return preview

        rel_path = preview.preview["file"]
        old_block = preview.preview["found_block"]

        file_path = os.path.join(workspace, rel_path)
        backup_path = file_path + ".bak"

        with open(file_path) as f:
            original = f.read()
        with open(backup_path, "w") as f:
            f.write(original)

        new_content = original.replace(old_block, new_transaction_text.strip(), 1)
        with open(file_path, "w") as f:
            f.write(new_content)

        is_clean, check_output = Beancount.bean_check(workspace)
        if not is_clean:
            with open(file_path, "w") as f:
                f.write(original)
            os.remove(backup_path)
            return ValidationFailed(
                error=check_output.strip(),
                remediation=(
                    "bean-check failed after replacement. "
                    "Adjust the transaction and try again."
                ),
            )

        os.remove(backup_path)
        Beancount.bean_format(workspace, file_path)

        git = GitService.commit_and_push(workspace, commit_message, github_token)
        if not git["ok"]:
            return DependencyUnavailable(
                error=f"Written but git commit failed: {git['error']}",
            )

        return CommitResult(
            outcome="Transaction updated, validated, and committed",
            result={
                "file": rel_path,
                "old_block": old_block,
                "new_block": new_transaction_text.strip(),
            },
            push_status=git["push"],
        )

    # ═══════════════════════════════════════════════════════════════════════
    # bulk_commit → preview_bulk + confirm_bulk
    # ═══════════════════════════════════════════════════════════════════════

    def preview_bulk(
        self,
        workspace: str,
        transactions_text: str = "",
        commit_message: str = "",
        transactions_file: str | None = None,
        whitelist: list[str] | None = None,
    ) -> Preview | InvariantViolation:
        """Validate a bulk-commit proposal. Returns Preview with proposal_id."""
        if transactions_file:
            try:
                with open(transactions_file, encoding="utf-8") as f:
                    transactions_text = f.read()
            except OSError as e:
                return InvariantViolation(
                    invariant="STAGING_ERROR",
                    severity="HARD",
                    provided=transactions_file,
                    remediation=f"Cannot read staging file: {e}",
                )
        elif not transactions_text:
            return InvariantViolation(
                invariant="MISSING_INPUT",
                severity="HARD",
                provided=None,
                remediation="Provide transactions_text or transactions_file.",
            )

        target = _ensure_agent_sidecar(workspace)

        violation = self.validate_accounts(
            workspace, transactions_text, whitelist,
        )
        if violation:
            return violation

        txn_lines = [
            line for line in transactions_text.splitlines()
            if re.match(r"^\d{4}-\d{2}-\d{2}\s+[*!]", line)
        ]
        txn_count = len(txn_lines)
        sample = "\n".join(txn_lines[:5])
        if txn_count > 5:
            sample += f"\n... ({txn_count - 5} more)"

        pid = f"prop_{uuid.uuid4().hex[:12]}"

        return Preview(
            proposal_id=pid,
            operation="bulk_commit",
            preview={
                "transaction_count": txn_count,
                "sample": sample,
                "target_file": target,
                "commit_message": commit_message,
            },
            message=f"{txn_count} transactions validated. Confirm to commit.",
        )

    def confirm_bulk(
        self,
        workspace: str,
        transactions_text: str = "",
        commit_message: str = "",
        transactions_file: str | None = None,
        github_token: str | None = None,
        whitelist: list[str] | None = None,
    ) -> CommitResult | ValidationFailed | DependencyUnavailable | InvariantViolation:
        """Validate and execute a bulk commit. Re-runs preview internally."""
        preview = self.preview_bulk(
            workspace, transactions_text, commit_message,
            transactions_file, whitelist,
        )
        if not isinstance(preview, Preview):
            return preview

        target = preview.preview["target_file"]
        txn_count = preview.preview["transaction_count"]

        target_path = os.path.join(workspace, target)
        backup_path = target_path + ".bak"

        with open(target_path) as f:
            original = f.read()
        with open(backup_path, "w") as f:
            f.write(original)

        with open(target_path, "a") as f:
            f.write(f"\n{transactions_text.strip()}\n")

        is_clean, check_output = Beancount.bean_check(workspace)
        if not is_clean:
            with open(target_path, "w") as f:
                f.write(original)
            os.remove(backup_path)
            return ValidationFailed(
                error=check_output.strip(),
                remediation="bean-check failed — all transactions auto-reverted.",
            )

        os.remove(backup_path)
        Beancount.bean_format(workspace, target_path)

        git = GitService.commit_and_push(workspace, commit_message, github_token)
        if not git["ok"]:
            return DependencyUnavailable(
                error=f"Written but git commit failed: {git['error']}",
            )

        if transactions_file and os.path.exists(transactions_file):
            os.remove(transactions_file)

        return CommitResult(
            outcome=f"{txn_count} transactions recorded and committed",
            result={
                "target_file": target,
                "transaction_count": txn_count,
            },
            push_status=git["push"],
        )

    # ═══════════════════════════════════════════════════════════════════════
    # Read operations (stateless)
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def get_balance(
        workspace: str, account: str, as_of_date: str | None = None,
    ) -> QueryResult:
        date_clause = f'AND date < "{as_of_date}"' if as_of_date else ""
        bql = (
            f'SELECT sum(position) AS balance '
            f'WHERE account ~ "^{account}$" {date_clause}'
        )
        rows, error = Beancount.run_bql_rows(workspace, bql)
        if error:
            return QueryResult(
                status="DEPENDENCY_UNAVAILABLE", error=error,
            )
        balance_raw = rows[0].get("balance", "").strip() if rows else ""
        return QueryResult(
            status="SUCCESS",
            account=account,
            as_of=as_of_date or "latest",
            balance=balance_raw if balance_raw else "0",
        )

    @staticmethod
    def find_transactions(
        workspace: str,
        account: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        narration_contains: str | None = None,
        limit: int = 20,
    ) -> QueryResult:
        filters = []
        if account:
            filters.append(f'account ~ "{account}"')
        if date_from:
            filters.append(f"date >= {date_from}")
        if date_to:
            filters.append(f"date <= {date_to}")
        if narration_contains:
            filters.append(f'narration ~ "{re.escape(narration_contains)}"')

        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        bql = (
            f"SELECT date, flag, payee, narration, account, position "
            f"{where} ORDER BY date DESC LIMIT {limit}"
        )

        rows, error = Beancount.run_bql_rows(workspace, bql)
        if error:
            return QueryResult(status="DEPENDENCY_UNAVAILABLE", error=error)

        return QueryResult(
            status="SUCCESS",
            count=len(rows),
            rows=rows,
            filters_applied={
                "account": account,
                "date_from": date_from,
                "date_to": date_to,
                "narration_contains": narration_contains,
                "limit": limit,
            },
        )

    @staticmethod
    def query_bql(workspace: str, bql: str) -> QueryResult:
        rows, error = Beancount.run_bql_rows(workspace, bql)
        if error:
            return QueryResult(status="ERROR", error=error, bql=bql)
        return QueryResult(status="SUCCESS", count=len(rows), rows=rows)

    @staticmethod
    def query_template(
        workspace: str,
        template_name: str,
        params: dict,
        templates_dir: str | None = None,
    ) -> QueryResult:
        if templates_dir is None:
            templates_dir = os.path.join(
                os.path.dirname(__file__), "..", "ledger", "query_templates",
            )

        available = sorted(
            f[:-4] for f in os.listdir(templates_dir) if f.endswith(".bql")
        )

        if template_name not in available:
            return QueryResult(
                status="ERROR",
                error=f"Unknown template '{template_name}'. Available: {available}",
            )

        template_path = os.path.join(templates_dir, f"{template_name}.bql")
        try:
            with open(template_path) as f:
                lines = [
                    line for line in f if not re.match(r"^--\s*\w+:", line)
                ]
            bql = "".join(lines).strip()
        except FileNotFoundError:
            return QueryResult(
                status="ERROR", error=f"Template file not found: {template_name}",
            )

        for key, value in params.items():
            bql = bql.replace(f"{{{key}}}", str(value))

        rows, error = Beancount.run_bql_rows(workspace, bql)
        if error:
            return QueryResult(status="ERROR", error=error, bql=bql)

        return QueryResult(
            status="SUCCESS",
            count=len(rows),
            rows=rows,
            template=template_name,
            params=params,
        )

    @staticmethod
    def preflight_report(workspace: str) -> PreflightResult:
        if not _check_sidecar_include(workspace):
            return PreflightResult(
                status="SETUP_REQUIRED",
                action=(
                    'Add include "agent_inc/main.beancount" to '
                    "data/main.beancount"
                ),
            )

        target = _ensure_agent_sidecar(workspace)
        is_clean, check_output = Beancount.bean_check(workspace)
        accounts = LedgerService.get_accounts(workspace)

        # Collect recent transactions
        recent = ""
        path = os.path.join(workspace, target)
        try:
            with open(path) as f:
                lines = f.readlines()
            txn_indices = [
                i for i, line in enumerate(lines)
                if re.match(r"^\d{4}-\d{2}-\d{2} ", line)
            ]
            start = (
                txn_indices[-5]
                if len(txn_indices) >= 5
                else (txn_indices[0] if txn_indices else 0)
            )
            recent = "".join(lines[start:]).strip()
        except OSError:
            pass

        return PreflightResult(
            status="CLEAN" if is_clean else "ERROR",
            target=target,
            accounts=accounts,
            errors=check_output.strip() if not is_clean else None,
            recent=recent,
        )
