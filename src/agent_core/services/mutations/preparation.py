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
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Callable

from beancount import loader

from ..approvals.contracts import PendingActionService
from ..beancount import Beancount, LedgerServiceError, _cfg, _repo_path
from ..inspection import preflight_report as _read_only_preflight_report
from ..queries import LedgerQueryService
from ..types import (
    CommitResult,
    DependencyUnavailable,
    InvariantViolation,
    LedgerConfig,
    PendingAction,
    PreflightResult,
    Preview,
    QueryResult,
    ValidationFailed,
    ValidationSummary,
)
from ..workspace import GitService
from .action_handlers import (
    MutationPreparationHandlerRegistry,
    extract_posting_accounts,
)
from .coordinator import MutationCoordinator
from .planners import MutationPlanner
from .plans import MutationOperation, MutationPlan

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DryRunResult:
    target_file: str | None
    validation: ValidationSummary
    failure: ValidationFailed | None = None


@dataclass(frozen=True)
class _ChangeSetReplay:
    touched_files: list[str]
    display_items: list[dict[str, object]]
    affected_accounts: list[str]
    transaction_count: int


def _include_line(entry_path: str, sidecar_main_path: str) -> str:
    relative = os.path.relpath(
        sidecar_main_path,
        start=PurePosixPath(entry_path).parent.as_posix(),
    ).replace(os.sep, "/")
    return f'include "{relative}"'


def _git_dependency_error(git: dict) -> DependencyUnavailable | None:
    if not git["ok"]:
        return DependencyUnavailable(
            error=f"Written but git commit failed: {git['error']}",
        )
    push = git.get("push")
    if isinstance(push, str) and push.startswith("PUSH_FAILED"):
        return DependencyUnavailable(
            error=f"Git commit succeeded locally but push failed: {push}",
            retryable=True,
        )
    return None


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
_OPEN_ACCOUNT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}\s+open\s+"
    r"((?:Assets|Liabilities|Equity|Income|Expenses)(?::[A-Za-z][A-Za-z0-9\-]+)+)",
    re.MULTILINE,
)
_AMOUNT_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*\s+[A-Z][A-Z0-9\-]+")
_CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9\-]*$")
_INVENTORY_AMOUNT_RE = re.compile(
    r"(?P<amount>[-+]?\d[\d,]*(?:\.\d+)?)\s+(?P<currency>[A-Z][A-Z0-9\-]*)"
)


def _summarize_validation_failure(output: str) -> ValidationSummary:
    lower = output.lower()
    messages: list[str]
    error_type = "beancount_validation_error"
    if "does not balance" in lower:
        error_type = "transaction_not_balanced"
        messages = [
            "One or more transactions do not balance.",
            "Check posting signs, commodities, and whether one posting should be inferred.",
        ]
    elif "syntax error" in lower or "parser" in lower:
        error_type = "syntax_error"
        messages = [
            "The draft contains Beancount syntax that could not be parsed.",
            "Check dates, quotes, indentation, directives, and posting lines.",
        ]
    elif "balance failed" in lower or "balance assertion" in lower:
        error_type = "balance_assertion_failed"
        messages = [
            "The draft changes ledger balances in a way that violates an assertion.",
            "Review whether the proposed mutation belongs in the sidecar ledger.",
        ]
    else:
        messages = [
            "The draft does not pass deterministic Beancount validation.",
            "Revise the proposed ledger text and run the mutation tool again.",
        ]

    error_count = max(1, len([line for line in output.splitlines() if line.strip()]))
    return ValidationSummary(
        status="failed",
        error_type=error_type,
        error_count=error_count,
        messages=messages,
        retryable=True,
    )


def _validation_failure(output: str, remediation: str) -> ValidationFailed:
    summary = _summarize_validation_failure(output)
    return ValidationFailed(
        error=summary.error_type or "beancount_validation_error",
        remediation=remediation,
        advisory=asdict(summary),
    )


def _validation_success(isolated: bool) -> ValidationSummary:
    return ValidationSummary(status="validated", isolated=isolated)


def _format_decimal(amount: Decimal) -> str:
    """Render a plain Beancount amount without exponent notation."""
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _parse_single_currency_balance(
    balance: str,
    currency: str,
) -> Decimal | InvariantViolation:
    if balance.strip() in {"", "0"}:
        return Decimal("0")
    positions = [
        (match.group("amount").replace(",", ""), match.group("currency"))
        for match in _INVENTORY_AMOUNT_RE.finditer(balance)
    ]
    if len(positions) != 1:
        return InvariantViolation(
            invariant="RECONCILIATION_MULTI_COMMODITY_BALANCE",
            severity="HARD",
            provided=balance,
            remediation=(
                "Balance reconciliation requires exactly one commodity in the "
                "account balance. Reconcile each commodity separately."
            ),
        )
    raw_amount, actual_currency = positions[0]
    if actual_currency != currency:
        return InvariantViolation(
            invariant="RECONCILIATION_CURRENCY_MISMATCH",
            severity="HARD",
            provided={"requested": currency, "actual": actual_currency},
            remediation="Use the account's balance commodity as the reconciliation currency.",
        )
    try:
        return Decimal(raw_amount)
    except InvalidOperation:
        return InvariantViolation(
            invariant="RECONCILIATION_BALANCE_PARSE",
            severity="HARD",
            provided=balance,
            remediation="Inspect the account balance and retry the reconciliation.",
        )


# ---------------------------------------------------------------------------
# Sidecar helpers
# ---------------------------------------------------------------------------


def _agent_sidecar_target_file(ledger_config: LedgerConfig | None = None) -> str:
    config = _cfg(ledger_config)
    today = date.today()
    chunk_name = f"{today.year}-{today.month:02d}.beancount"
    return f"{config.sidecar_write_dir}/{chunk_name}"


def _check_sidecar_include(workspace: str, ledger_config: LedgerConfig | None = None) -> bool:
    config = _cfg(ledger_config)
    main = _repo_path(workspace, config.entry_path)
    include = _include_line(config.entry_path, config.sidecar_main_path)
    try:
        with open(main) as f:
            return include in f.read()
    except OSError:
        return False


def _ensure_agent_sidecar(workspace: str, ledger_config: LedgerConfig | None = None) -> str:
    config = _cfg(ledger_config)
    today = date.today()
    chunk_name = f"{today.year}-{today.month:02d}.beancount"
    agent_dir = _repo_path(workspace, config.sidecar_write_dir)
    os.makedirs(agent_dir, exist_ok=True)

    chunk_path = os.path.join(agent_dir, chunk_name)
    changed = False
    if not os.path.exists(chunk_path):
        with open(chunk_path, "w") as f:
            f.write(f"; Agent-generated transactions — {today.year}-{today.month:02d}\n")
        changed = True

    agg_path = _repo_path(workspace, config.sidecar_main_path)
    os.makedirs(os.path.dirname(agg_path), exist_ok=True)
    include_line = f'include "{chunk_name}"\n'
    if os.path.exists(agg_path):
        with open(agg_path) as f:
            existing = f.read()
        if chunk_name not in existing:
            with open(agg_path, "a") as f:
                f.write(include_line)
            changed = True
    else:
        with open(agg_path, "w") as f:
            f.write("; Agent sidecar — auto-managed, do not edit manually\n")
            f.write(include_line)
        changed = True

    if changed:
        Beancount.invalidate_cache(workspace, config)

    return f"{config.sidecar_write_dir}/{chunk_name}"


def _copy_workspace_for_dry_run(workspace: str, target: str) -> None:
    shutil.copytree(
        workspace,
        target,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
        ),
    )


def _run_isolated_validation(
    workspace: str,
    ledger_config: LedgerConfig | None,
    mutator: Callable[[str], str | None],
    remediation: str,
) -> _DryRunResult:
    config = _cfg(ledger_config)
    try:
        with tempfile.TemporaryDirectory(prefix="beanpilot-dry-run-") as tmp:
            dry_workspace = os.path.join(tmp, "workspace")
            _copy_workspace_for_dry_run(workspace, dry_workspace)
            try:
                target_file = mutator(dry_workspace)
                Beancount.invalidate_cache(dry_workspace, config)
                is_clean, check_output = Beancount.bean_check(dry_workspace, config)
                if not is_clean:
                    return _DryRunResult(
                        target_file=target_file,
                        validation=_summarize_validation_failure(check_output),
                        failure=_validation_failure(check_output, remediation),
                    )
                return _DryRunResult(
                    target_file=target_file,
                    validation=_validation_success(isolated=True),
                )
            finally:
                Beancount.invalidate_workspace(dry_workspace)
    except OSError as exc:
        raise LedgerServiceError("Dry-run validation workspace unavailable") from exc


def _run_plan_validation(
    workspace: str,
    ledger_config: LedgerConfig | None,
    plan: MutationPlan,
) -> _DryRunResult:
    """Validate a replayable plan through the shared isolated coordinator."""
    try:
        result = MutationCoordinator().validate(workspace, plan, ledger_config)
    except OSError as exc:
        raise LedgerServiceError("Dry-run validation workspace unavailable") from exc
    if result.check_output:
        return _DryRunResult(
            target_file=result.touched_files[-1] if result.touched_files else None,
            validation=_summarize_validation_failure(result.check_output),
            failure=_validation_failure(result.check_output, plan.remediation),
        )
    return _DryRunResult(
        target_file=result.touched_files[-1] if result.touched_files else None,
        validation=_validation_success(isolated=True),
    )


def _apply_plan(
    workspace: str,
    ledger_config: LedgerConfig | None,
    plan: MutationPlan,
    repo_url: str,
    git_service: GitService,
    github_token: str | None,
) -> tuple[tuple[str, ...], dict, ValidationFailed | InvariantViolation | None]:
    try:
        touched, git, output = MutationCoordinator().apply_and_publish(
            workspace, plan, repo_url, git_service, github_token, ledger_config
        )
    except OSError as exc:
        raise LedgerServiceError("Ledger mutation apply workspace unavailable") from exc
    if output == "MUTATION_PRECONDITION_FAILED":
        return (
            touched,
            git,
            InvariantViolation(
                invariant="MUTATION_PRECONDITION_FAILED",
                severity="HARD",
                remediation="The ledger changed after preview. Prepare and review a new action.",
            ),
        )
    if output:
        return touched, git, _validation_failure(output, plan.remediation)
    return touched, git, None


def _append_to_sidecar(
    workspace: str,
    text: str,
    ledger_config: LedgerConfig | None = None,
) -> str:
    target = _ensure_agent_sidecar(workspace, ledger_config)
    target_path = os.path.join(workspace, target)
    with open(target_path, "a") as f:
        f.write(f"\n{text.strip()}\n")
    Beancount.invalidate_cache(workspace, ledger_config)
    return target


def _write_open_directive(
    workspace: str,
    account_name: str,
    directive_text: str,
    ledger_config: LedgerConfig | None = None,
) -> str:
    config = _cfg(ledger_config)
    _ensure_agent_sidecar(workspace, config)
    main_path = _repo_path(workspace, config.sidecar_main_path)

    with open(main_path) as f:
        original = f.read()

    directive_lines = directive_text.split("\n")
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
    Beancount.invalidate_cache(workspace, config)
    return config.sidecar_main_path


def _read_repo_file(workspace: str, rel_path: str) -> str | None:
    path = _repo_path(workspace, rel_path)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _write_repo_file(workspace: str, rel_path: str, content: str | None) -> None:
    path = _repo_path(workspace, rel_path)
    if content is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _restore_repo_files(workspace: str, originals: dict[str, str | None]) -> None:
    for rel_path, content in originals.items():
        _write_repo_file(workspace, rel_path, content)
    Beancount.invalidate_workspace(workspace)


def _replace_transaction_block(
    workspace: str,
    rel_path: str,
    old_block: str,
    new_transaction_text: str,
    ledger_config: LedgerConfig | None = None,
) -> str:
    file_path = os.path.join(workspace, rel_path)
    with open(file_path) as f:
        original = f.read()
    new_content = original.replace(old_block, new_transaction_text.strip(), 1)
    with open(file_path, "w") as f:
        f.write(new_content)
    Beancount.invalidate_cache(workspace, ledger_config)
    return rel_path


# ---------------------------------------------------------------------------
# Mutation preparation and legacy compatibility implementation
# ---------------------------------------------------------------------------


class MutationPreparationService:
    """Prepare mutation contracts and retain legacy direct-confirm behavior.

    Write operations follow preview→confirm split:
      - preview_* returns Preview (validates, proposal_id is informational)
      - confirm_* accepts full payload, calls preview_* internally for re-validation,
        then executes directly

    Query operations are owned by LedgerQueryService.
    """

    def __init__(self, handler_registry: MutationPreparationHandlerRegistry | None = None) -> None:
        self._handler_registry = handler_registry or MutationPreparationHandlerRegistry()

    @staticmethod
    def _extract_accounts(transaction_text: str) -> list[str]:
        # Compatibility helper. New transaction preparation owns this read in
        # TransactionCommitPreparationHandler.
        return extract_posting_accounts(transaction_text)

    @staticmethod
    def _serialized_plan(
        workspace: str, plan: MutationPlan, ledger_config: LedgerConfig | None
    ) -> dict[str, object]:
        """Seal the exact approved operation list with workspace preconditions."""
        return MutationCoordinator.seal(workspace, plan, ledger_config).to_spec()

    @staticmethod
    def validate_accounts(
        workspace: str,
        transaction_text: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> InvariantViolation | None:
        used = MutationPreparationService._extract_accounts(transaction_text)
        valid = set(LedgerQueryService.get_accounts(workspace, ledger_config))

        unknown = [a for a in used if a not in valid]
        if unknown:
            return InvariantViolation(
                invariant="ACCOUNT_WHITELIST",
                severity="HARD",
                provided=unknown,
                remediation=("Unknown accounts detected. Use open_account to create them first."),
                detail={"valid_accounts": sorted(valid)},
            )

        if whitelist:
            out_of_scope = [a for a in used if not any(a.startswith(w) for w in whitelist)]
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

    def _preview_registered(
        self,
        action_type: str,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: object,
    ) -> Preview | InvariantViolation | ValidationFailed:
        """Shared canonical preparation flow: dispatch then isolated validation.

        Action-specific policy, lookup, preview facts, and plan construction
        intentionally remain in the registered handler.
        """
        prepared = self._handler_registry.get(action_type).build(workspace, ledger_config, **kwargs)
        if isinstance(prepared, InvariantViolation):
            return prepared
        dry_run = _run_plan_validation(workspace, ledger_config, prepared.plan)
        if dry_run.failure:
            return dry_run.failure
        return prepared.preview(
            dry_run.validation,
            dry_run.target_file or _agent_sidecar_target_file(ledger_config),
        )

    def _prepare_registered(
        self,
        action_type: str,
        workspace: str,
        ledger_config: LedgerConfig | None = None,
        **kwargs: object,
    ) -> PendingAction | InvariantViolation | ValidationFailed:
        """Dispatch, validate, seal, and construct an action-owned pending payload."""
        prepared = self._handler_registry.get(action_type).build(workspace, ledger_config, **kwargs)
        if isinstance(prepared, InvariantViolation):
            return prepared
        dry_run = _run_plan_validation(workspace, ledger_config, prepared.plan)
        if dry_run.failure:
            return dry_run.failure
        preview = prepared.preview(
            dry_run.validation,
            dry_run.target_file or _agent_sidecar_target_file(ledger_config),
        )
        sealed_plan = self._serialized_plan(workspace, prepared.plan, ledger_config)
        return prepared.pending_action(sealed_plan, preview)

    # ═══════════════════════════════════════════════════════════════════════
    # commit_transaction → preview_commit + confirm_commit
    # ═══════════════════════════════════════════════════════════════════════

    def preview_commit(
        self,
        workspace: str,
        transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> Preview | InvariantViolation | ValidationFailed:
        """Compatibility entry point for registered transaction preparation."""
        return self._preview_registered(
            "commit_transaction",
            workspace,
            ledger_config,
            transaction_text=transaction_text,
            commit_message=commit_message,
            whitelist=whitelist,
        )

    def prepare_commit(
        self,
        workspace: str,
        transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> PendingAction | InvariantViolation | ValidationFailed:
        return self._prepare_registered(
            "commit_transaction",
            workspace,
            ledger_config,
            transaction_text=transaction_text,
            commit_message=commit_message,
            whitelist=whitelist,
        )

    def confirm_commit(
        self,
        workspace: str,
        transaction_text: str,
        commit_message: str,
        repo_url: str,
        git_service: GitService,
        github_token: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> CommitResult | ValidationFailed | DependencyUnavailable | InvariantViolation:
        """Validate and execute a transaction commit. Re-runs preview internally."""
        preview = self.preview_commit(
            workspace, transaction_text, commit_message, whitelist, ledger_config
        )
        if not isinstance(preview, Preview):
            return preview

        plan = MutationPlanner.commit(transaction_text, commit_message)
        touched, git, failure = _apply_plan(
            workspace, ledger_config, plan, repo_url, git_service, github_token
        )
        if failure:
            return failure
        if dependency_error := _git_dependency_error(git):
            return dependency_error

        return CommitResult(
            outcome="Transaction recorded, validated, and committed",
            result={
                "target_file": touched[-1],
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
        ledger_config: LedgerConfig | None = None,
    ) -> Preview | InvariantViolation | ValidationFailed:
        """Compatibility entry point for registered account-open preparation."""
        return self._preview_registered(
            "open_account",
            workspace,
            ledger_config,
            account_name=account_name,
            currency=currency,
            open_date=open_date,
            display_name=display_name,
        )

    def confirm_open(
        self,
        workspace: str,
        account_name: str,
        currency: str | None,
        open_date: str,
        repo_url: str,
        git_service: GitService,
        display_name: str | None = None,
        github_token: str | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> CommitResult | ValidationFailed | DependencyUnavailable | InvariantViolation:
        """Validate and execute an open-account. Re-runs preview internally."""
        preview = self.preview_open(
            workspace, account_name, currency, open_date, display_name, ledger_config
        )
        if not isinstance(preview, Preview):
            return preview

        directive_text = preview.preview["directive"]

        config = _cfg(ledger_config)
        plan = MutationPlanner.open_account(account_name, str(directive_text))
        _, git, failure = _apply_plan(workspace, config, plan, repo_url, git_service, github_token)
        if failure:
            return failure
        if dependency_error := _git_dependency_error(git):
            return dependency_error

        return CommitResult(
            outcome=f"Account '{account_name}' opened and committed",
            result={
                "account": account_name,
                "currency": currency,
                "open_date": open_date,
                "file": config.sidecar_main_path,
            },
            push_status=git["push"],
        )

    def prepare_open(
        self,
        workspace: str,
        account_name: str,
        currency: str | None,
        open_date: str,
        display_name: str | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> PendingAction | InvariantViolation | ValidationFailed:
        return self._prepare_registered(
            "open_account",
            workspace,
            ledger_config,
            account_name=account_name,
            currency=currency,
            open_date=open_date,
            display_name=display_name,
        )

    # ═══════════════════════════════════════════════════════════════════════
    # update_transaction → preview_update + confirm_update
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def find_transaction_block(
        workspace: str,
        target_date: str,
        narration: str,
        ledger_config: LedgerConfig | None = None,
    ) -> list[tuple[str, str, str]]:
        """Search beancount files for a transaction matching date + narration.

        Uses bean-query to validate the transaction exists across ALL included
        files, then walks data/ for .beancount files to extract raw blocks.

        Returns list of (rel_path, file_content, raw_block) tuples.
        """
        escaped_narration = narration.replace('"', '\\"')
        bql = (
            f"SELECT DISTINCT date, narration "
            f'WHERE date = {target_date} AND narration ~ "{escaped_narration}"'
        )
        rows, error = Beancount.run_bql_rows(workspace, bql, ledger_config)
        if error or not rows:
            return []

        header_re = re.compile(
            rf"^{re.escape(target_date)}\s+[*!].*?{re.escape(narration)}",
            re.MULTILINE,
        )
        results: list[tuple[str, str, str]] = []
        data_dir = workspace

        try:
            for dirpath, dirnames, filenames in os.walk(data_dir):
                dirnames[:] = [d for d in dirnames if d not in {".git", ".venv"}]
                for fname in sorted(filenames):
                    if not fname.endswith(".beancount"):
                        continue
                    abs_path = os.path.join(dirpath, fname)
                    rel = os.path.relpath(abs_path, workspace)
                    try:
                        with open(abs_path) as f:
                            content = f.read()
                    except OSError:
                        continue
                    for m in header_re.finditer(content):
                        block_start = m.start()
                        rest = content[block_start:]
                        end_match = re.search(r"\n[ \t]*\n", rest)
                        raw = rest[: end_match.start()].rstrip() if end_match else rest.rstrip()
                        results.append((rel, content, raw))
        except OSError:
            pass

        return results

    @staticmethod
    def _detect_value_change(old_text: str, new_text: str) -> dict | None:
        old_amounts = set(_AMOUNT_RE.findall(old_text))
        new_amounts = set(_AMOUNT_RE.findall(new_text))
        old_accounts = {m.group(0).strip() for m in _POSTING_ACCOUNT_RE.finditer(old_text)}
        new_accounts = {m.group(0).strip() for m in _POSTING_ACCOUNT_RE.finditer(new_text)}
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
        ledger_config: LedgerConfig | None = None,
    ) -> Preview | InvariantViolation | ValidationFailed:
        """Find and validate a replacement transaction in an isolated dry-run."""
        matches = self.find_transaction_block(workspace, target_date, narration, ledger_config)

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
                    "matches_found": [{"file": rel, "block": block} for rel, _, block in matches],
                },
            )

        rel_path, _, old_block = matches[0]

        violation = self.validate_accounts(
            workspace,
            new_transaction_text,
            whitelist,
            ledger_config,
        )
        if violation:
            return violation

        advisory = self._detect_value_change(old_block, new_transaction_text)
        plan = MutationPlanner.update(rel_path, old_block, new_transaction_text, commit_message)
        dry_run = _run_plan_validation(workspace, ledger_config, plan)
        if dry_run.failure:
            return dry_run.failure

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
                "validation": asdict(dry_run.validation),
            },
            message=("Replacement passed dry-run validation. Request explicit approval."),
        )

    def confirm_update(
        self,
        workspace: str,
        target_date: str,
        narration: str,
        new_transaction_text: str,
        commit_message: str,
        repo_url: str,
        git_service: GitService,
        github_token: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> CommitResult | ValidationFailed | DependencyUnavailable | InvariantViolation:
        """Validate and execute a transaction update. Re-runs preview internally."""
        preview = self.preview_update(
            workspace,
            target_date,
            narration,
            new_transaction_text,
            commit_message,
            whitelist,
            ledger_config,
        )
        if not isinstance(preview, Preview):
            return preview

        rel_path = preview.preview["file"]
        old_block = preview.preview["found_block"]

        plan = MutationPlanner.update(
            str(rel_path), str(old_block), new_transaction_text, commit_message
        )
        _, git, failure = _apply_plan(
            workspace, ledger_config, plan, repo_url, git_service, github_token
        )
        if failure:
            return failure
        if dependency_error := _git_dependency_error(git):
            return dependency_error

        return CommitResult(
            outcome="Transaction updated, validated, and committed",
            result={
                "file": rel_path,
                "old_block": old_block,
                "new_block": new_transaction_text.strip(),
            },
            push_status=git["push"],
        )

    def prepare_update(
        self,
        workspace: str,
        target_date: str,
        narration: str,
        new_transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> PendingAction | InvariantViolation | ValidationFailed:
        preview = self.preview_update(
            workspace,
            target_date,
            narration,
            new_transaction_text,
            commit_message,
            whitelist,
            ledger_config,
        )
        if not isinstance(preview, Preview):
            return preview
        return PendingActionService.create_pending_action(
            action_type="update_transaction",
            execution_spec={
                "mutation_plan": self._serialized_plan(
                    workspace,
                    MutationPlanner.update(
                        str(preview.preview["file"]),
                        str(preview.preview["found_block"]),
                        new_transaction_text,
                        commit_message,
                    ),
                    ledger_config,
                ),
                "target_date": target_date,
                "narration": narration,
                "new_transaction_text": new_transaction_text,
                "commit_message": commit_message,
            },
            display={
                "kind": "transaction_update_preview",
                "summary": "Update a transaction",
                "diff": new_transaction_text,
                "preview": preview.preview,
            },
            validation={
                "status": "validated",
                "file": preview.preview.get("file"),
                "advisory": preview.preview.get("advisory"),
                "dry_run": preview.preview.get("validation"),
            },
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
        ledger_config: LedgerConfig | None = None,
    ) -> Preview | InvariantViolation | ValidationFailed:
        """Validate a bulk-commit proposal in an isolated dry-run."""
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

        violation = self.validate_accounts(
            workspace,
            transactions_text,
            whitelist,
            ledger_config,
        )
        if violation:
            return violation

        plan = MutationPlanner.bulk(transactions_text, commit_message)
        dry_run = _run_plan_validation(workspace, ledger_config, plan)
        if dry_run.failure:
            return dry_run.failure

        target = dry_run.target_file or _agent_sidecar_target_file(ledger_config)

        txn_lines = [
            line
            for line in transactions_text.splitlines()
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
                "validation": asdict(dry_run.validation),
            },
            message=f"{txn_count} transactions passed dry-run validation.",
        )

    def confirm_bulk(
        self,
        workspace: str,
        transactions_text: str = "",
        commit_message: str = "",
        repo_url: str = "",
        git_service: GitService | None = None,
        transactions_file: str | None = None,
        github_token: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> CommitResult | ValidationFailed | DependencyUnavailable | InvariantViolation:
        """Validate and execute a bulk commit. Re-runs preview internally."""
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

        preview = self.preview_bulk(
            workspace,
            transactions_text,
            commit_message,
            None,
            whitelist,
            ledger_config,
        )
        if not isinstance(preview, Preview):
            return preview

        txn_count = preview.preview["transaction_count"]

        if git_service is None:
            return DependencyUnavailable(error="Git service is not configured")
        plan = MutationPlanner.bulk(transactions_text, commit_message)
        touched, git, failure = _apply_plan(
            workspace, ledger_config, plan, repo_url, git_service, github_token
        )
        if failure:
            return failure
        if dependency_error := _git_dependency_error(git):
            return dependency_error

        if transactions_file and os.path.exists(transactions_file):
            os.remove(transactions_file)

        return CommitResult(
            outcome=f"{txn_count} transactions recorded and committed",
            result={
                "target_file": touched[-1],
                "transaction_count": txn_count,
            },
            push_status=git["push"],
        )

    def prepare_bulk(
        self,
        workspace: str,
        transactions_text: str = "",
        commit_message: str = "",
        transactions_file: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> PendingAction | InvariantViolation | ValidationFailed:
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
            transactions_file = None

        preview = self.preview_bulk(
            workspace,
            transactions_text,
            commit_message,
            transactions_file,
            whitelist,
            ledger_config,
        )
        if not isinstance(preview, Preview):
            return preview
        return PendingActionService.create_pending_action(
            action_type="bulk_commit",
            execution_spec={
                "mutation_plan": self._serialized_plan(
                    workspace,
                    MutationPlanner.bulk(transactions_text, commit_message),
                    ledger_config,
                ),
                "transactions_text": transactions_text,
                "commit_message": commit_message,
            },
            display={
                "kind": "bulk_import_preview",
                "summary": "Record multiple transactions",
                "diff": preview.preview.get("sample", ""),
                "preview": preview.preview,
            },
            validation={
                "status": "validated",
                "transaction_count": preview.preview.get("transaction_count", 0),
                "target_file": preview.preview.get("target_file"),
                "dry_run": preview.preview.get("validation"),
            },
        )

    # ═══════════════════════════════════════════════════════════════════════
    # change_set → ordered open_account + commit_transaction operations
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _operation_type(operation: dict[str, object]) -> str:
        value = operation.get("type") or operation.get("operation")
        return str(value or "")

    @staticmethod
    def _operation_error(
        *,
        operation_index: int,
        invariant: str,
        provided: object,
        remediation: str,
        detail: dict[str, object] | None = None,
    ) -> InvariantViolation:
        return InvariantViolation(
            invariant=invariant,
            severity="HARD",
            provided=provided,
            remediation=remediation,
            detail={"operation_index": operation_index, **(detail or {})},
        )

    @staticmethod
    def _change_set_plan_from_operations(
        operations: list[dict[str, object]], commit_message: str
    ) -> MutationPlan:
        """Build the exact replay plan used for both preview and approved apply."""
        plan_operations: list[MutationOperation] = []
        for operation in operations:
            operation_type = MutationPreparationService._operation_type(operation)
            if operation_type == "open_account":
                account_name = str(operation.get("account_name") or "")
                directive = f"{operation.get('open_date') or ''} open {account_name}"
                currency = operation.get("currency")
                if isinstance(currency, str) and currency:
                    directive += f"  {currency}"
                display_name = operation.get("display_name")
                if isinstance(display_name, str) and display_name:
                    directive += f'\n  name: "{display_name}"'
                plan_operations.append(
                    MutationOperation(kind="open", account_name=account_name, text=directive)
                )
            elif operation_type == "commit_transaction":
                plan_operations.append(
                    MutationOperation(
                        kind="append", text=str(operation.get("transaction_text") or "")
                    )
                )
            else:
                raise ValueError(f"Unsupported change-set operation: {operation_type}")
        return MutationPlanner.change_set(plan_operations, commit_message)

    def _replay_change_set_operations(
        self,
        workspace: str,
        operations: list[dict[str, object]],
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> _ChangeSetReplay | InvariantViolation:
        touched: list[str] = []
        display_items: list[dict[str, object]] = []
        affected_accounts: set[str] = set()
        transaction_count = 0

        for index, operation in enumerate(operations):
            operation_type = self._operation_type(operation)
            if operation_type == "open_account":
                account_name = str(operation.get("account_name") or "")
                currency = (
                    operation.get("currency")
                    if isinstance(operation.get("currency"), str)
                    else None
                )
                open_date = str(operation.get("open_date") or "")
                display_name = (
                    operation.get("display_name")
                    if isinstance(operation.get("display_name"), str)
                    else None
                )
                if not _ACCOUNT_NAME_RE.match(account_name):
                    return self._operation_error(
                        operation_index=index,
                        invariant="ACCOUNT_NAME_FORMAT",
                        provided=account_name,
                        remediation=(
                            "Account names must follow Beancount format: "
                            "Type:Component (e.g. Assets:Liquid:Bank:NewAccount)."
                        ),
                    )
                existing = LedgerQueryService.get_accounts(workspace, ledger_config)
                if account_name in existing:
                    return self._operation_error(
                        operation_index=index,
                        invariant="ACCOUNT_ALREADY_EXISTS",
                        provided=account_name,
                        remediation=f"Account '{account_name}' already exists.",
                    )

                currency_part = f"  {currency}" if currency else ""
                directive_lines = [f"{open_date} open {account_name}{currency_part}"]
                if display_name:
                    directive_lines.append(f'  name: "{display_name}"')
                directive_text = "\n".join(directive_lines)
                target = _write_open_directive(
                    workspace, account_name, directive_text, ledger_config
                )
                if target not in touched:
                    touched.append(target)
                affected_accounts.add(account_name)
                display_items.append(
                    {
                        "operation_index": index,
                        "type": "open_account",
                        "summary": f"Open {account_name}",
                        "diff": directive_text,
                        "target_file": target,
                    }
                )
            elif operation_type == "commit_transaction":
                transaction_text = str(operation.get("transaction_text") or "")
                violation = self.validate_accounts(
                    workspace, transaction_text, whitelist, ledger_config
                )
                if violation:
                    return InvariantViolation(
                        invariant=violation.invariant,
                        severity=violation.severity,
                        provided=violation.provided,
                        remediation=violation.remediation,
                        detail={
                            "operation_index": index,
                            **violation.detail,
                        },
                    )
                target = _append_to_sidecar(workspace, transaction_text, ledger_config)
                if target not in touched:
                    touched.append(target)
                accounts = self._extract_accounts(transaction_text)
                affected_accounts.update(accounts)
                transaction_count += 1
                display_items.append(
                    {
                        "operation_index": index,
                        "type": "commit_transaction",
                        "summary": "Record a transaction",
                        "diff": transaction_text,
                        "target_file": target,
                        "accounts": accounts,
                    }
                )
            else:
                return self._operation_error(
                    operation_index=index,
                    invariant="UNSUPPORTED_CHANGE_SET_OPERATION",
                    provided=operation_type,
                    remediation=(
                        "Only open_account and commit_transaction operations "
                        "are supported in change sets."
                    ),
                )

        return _ChangeSetReplay(
            touched_files=touched,
            display_items=display_items,
            affected_accounts=sorted(affected_accounts),
            transaction_count=transaction_count,
        )

    def prepare_change_set(
        self,
        workspace: str,
        operations: list[dict[str, object]],
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> PendingAction | InvariantViolation | ValidationFailed:
        if not operations:
            return InvariantViolation(
                invariant="MISSING_OPERATIONS",
                severity="HARD",
                provided=operations,
                remediation="Provide at least one change-set operation.",
            )
        known_accounts = set(LedgerQueryService.get_accounts(workspace, ledger_config))
        display_items: list[dict[str, object]] = []
        affected_accounts: set[str] = set()
        transaction_count = 0
        for index, operation in enumerate(operations):
            operation_type = self._operation_type(operation)
            if operation_type == "open_account":
                account_name = str(operation.get("account_name") or "")
                if not _ACCOUNT_NAME_RE.match(account_name):
                    return self._operation_error(
                        operation_index=index,
                        invariant="ACCOUNT_NAME_FORMAT",
                        provided=account_name,
                        remediation=(
                            "Account names must follow Beancount format: "
                            "Type:Component (e.g. Assets:Liquid:Bank:NewAccount)."
                        ),
                    )
                if account_name in known_accounts:
                    return self._operation_error(
                        operation_index=index,
                        invariant="ACCOUNT_ALREADY_EXISTS",
                        provided=account_name,
                        remediation=f"Account '{account_name}' already exists.",
                    )
                currency = operation.get("currency")
                directive = f"{operation.get('open_date') or ''} open {account_name}"
                if isinstance(currency, str) and currency:
                    directive += f"  {currency}"
                display_name = operation.get("display_name")
                if isinstance(display_name, str) and display_name:
                    directive += f'\n  name: "{display_name}"'
                display_items.append(
                    {
                        "operation_index": index,
                        "type": "open_account",
                        "summary": f"Open {account_name}",
                        "diff": directive,
                        "target_file": _cfg(ledger_config).sidecar_main_path,
                    }
                )
                known_accounts.add(account_name)
                affected_accounts.add(account_name)
            elif operation_type == "commit_transaction":
                transaction_text = str(operation.get("transaction_text") or "")
                accounts = self._extract_accounts(transaction_text)
                unknown = [account for account in accounts if account not in known_accounts]
                if unknown:
                    return self._operation_error(
                        operation_index=index,
                        invariant="ACCOUNT_WHITELIST",
                        provided=unknown,
                        remediation=(
                            "Unknown accounts detected. Use open_account to create them first."
                        ),
                        detail={"valid_accounts": sorted(known_accounts)},
                    )
                if whitelist:
                    out_of_scope = [
                        account
                        for account in accounts
                        if not any(account.startswith(prefix) for prefix in whitelist)
                    ]
                    if out_of_scope:
                        return self._operation_error(
                            operation_index=index,
                            invariant="CONVERSATION_SCOPE",
                            provided=out_of_scope,
                            remediation="Use accounts within the allowed prefixes.",
                            detail={"allowed_prefixes": whitelist},
                        )
                display_items.append(
                    {
                        "operation_index": index,
                        "type": "commit_transaction",
                        "summary": "Record a transaction",
                        "diff": transaction_text,
                        "target_file": _agent_sidecar_target_file(ledger_config),
                        "accounts": accounts,
                    }
                )
                affected_accounts.update(accounts)
                transaction_count += 1
            else:
                return self._operation_error(
                    operation_index=index,
                    invariant="UNSUPPORTED_CHANGE_SET_OPERATION",
                    provided=operation_type,
                    remediation=(
                        "Only open_account and commit_transaction operations "
                        "are supported in change sets."
                    ),
                )

        plan = self._change_set_plan_from_operations(operations, commit_message)
        dry_run = _run_plan_validation(workspace, ledger_config, plan)
        if dry_run.failure:
            return dry_run.failure
        diff = "\n\n".join(str(item["diff"]) for item in display_items)
        return PendingActionService.create_pending_action(
            action_type="change_set",
            execution_spec={
                "mutation_plan": self._serialized_plan(workspace, plan, ledger_config),
                "operations": operations,
                "commit_message": commit_message,
            },
            display={
                "kind": "change_set_preview",
                "summary": f"Apply {len(operations)} related ledger changes",
                "diff": diff,
                "items": display_items,
            },
            validation={
                "status": "validated",
                "operation_count": len(operations),
                "transaction_count": transaction_count,
                "accounts": sorted(affected_accounts),
                "target_files": list(
                    dict.fromkeys(str(item["target_file"]) for item in display_items)
                ),
                "dry_run": asdict(_validation_success(isolated=True)),
            },
        )

    def confirm_change_set(
        self,
        workspace: str,
        operations: list[dict[str, object]],
        commit_message: str,
        repo_url: str,
        git_service: GitService,
        github_token: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> CommitResult | ValidationFailed | DependencyUnavailable | InvariantViolation:
        if not operations:
            return InvariantViolation(
                invariant="MISSING_OPERATIONS",
                severity="HARD",
                provided=operations,
                remediation="Provide at least one change-set operation.",
            )

        prepared = self.prepare_change_set(
            workspace, operations, commit_message, whitelist, ledger_config
        )
        if not isinstance(prepared, PendingAction):
            return prepared
        plan = self._change_set_plan_from_operations(operations, commit_message)
        touched, git, failure = _apply_plan(
            workspace, ledger_config, plan, repo_url, git_service, github_token
        )
        if failure:
            return failure
        if dependency_error := _git_dependency_error(git):
            return dependency_error

        transaction_count = sum(
            self._operation_type(operation) == "commit_transaction" for operation in operations
        )
        return CommitResult(
            outcome=f"{len(operations)} ledger changes validated and committed",
            result={
                "operation_count": len(operations),
                "transaction_count": transaction_count,
                "target_files": list(touched),
            },
            push_status=git["push"],
        )

    # ═══════════════════════════════════════════════════════════════════════
    # balance_reconciliation → calculate / preview / prepare / confirm
    # ═══════════════════════════════════════════════════════════════════════

    def calculate_balance_adjustment(
        self,
        workspace: str,
        observed_date: str,
        account: str,
        amount: str,
        currency: str,
        cutoff: str = "end_of_day",
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult | InvariantViolation | ValidationFailed:
        """Calculate a signed reconciliation difference without proposing a write."""
        if cutoff not in {"end_of_day", "start_of_day"}:
            return InvariantViolation(
                invariant="RECONCILIATION_CUTOFF",
                severity="HARD",
                provided=cutoff,
                remediation="Use end_of_day or start_of_day for the observed balance cutoff.",
            )
        try:
            parsed_observed_date = date.fromisoformat(observed_date)
        except ValueError:
            return InvariantViolation(
                invariant="RECONCILIATION_DATE_FORMAT",
                severity="HARD",
                provided=observed_date,
                remediation="Provide an ISO date in YYYY-MM-DD format.",
            )
        if not _ACCOUNT_NAME_RE.match(account):
            return InvariantViolation(
                invariant="ACCOUNT_NAME_FORMAT",
                severity="HARD",
                provided=account,
                remediation="Provide a full Beancount account name.",
            )
        if not _CURRENCY_RE.match(currency):
            return InvariantViolation(
                invariant="RECONCILIATION_CURRENCY_FORMAT",
                severity="HARD",
                provided=currency,
                remediation="Provide an uppercase Beancount commodity symbol.",
            )
        try:
            target_amount = Decimal(amount)
        except (InvalidOperation, ValueError):
            return InvariantViolation(
                invariant="RECONCILIATION_AMOUNT_FORMAT",
                severity="HARD",
                provided=amount,
                remediation="Provide a decimal target amount without currency symbols.",
            )

        existing_accounts = set(LedgerQueryService.get_accounts(workspace, ledger_config))
        if account not in existing_accounts:
            return InvariantViolation(
                invariant="ACCOUNT_WHITELIST",
                severity="HARD",
                provided=[account],
                remediation="Open the account before preparing a reconciliation.",
            )
        assertion_date = (
            parsed_observed_date + timedelta(days=1)
            if cutoff == "end_of_day"
            else parsed_observed_date
        ).isoformat()
        balance_result = self._get_reconciliation_balance(
            workspace, account, assertion_date, ledger_config
        )
        if balance_result.status != "SUCCESS":
            return ValidationFailed(
                error="reconciliation_balance_query_failed",
                remediation="Resolve the ledger query error and prepare the reconciliation again.",
            )
        current_amount = _parse_single_currency_balance(balance_result.balance or "0", currency)
        if isinstance(current_amount, InvariantViolation):
            return current_amount

        adjustment = target_amount - current_amount
        return QueryResult(
            status="SUCCESS",
            account=account,
            as_of=assertion_date,
            balance=f"{_format_decimal(current_amount)} {currency}",
            rows=[
                {
                    "observed_date": observed_date,
                    "cutoff": cutoff,
                    "assertion_date": assertion_date,
                    "ledger_balance": f"{_format_decimal(current_amount)} {currency}",
                    "observed_balance": f"{_format_decimal(target_amount)} {currency}",
                    "unexplained_difference": f"{_format_decimal(adjustment)} {currency}",
                }
            ],
        )

    @staticmethod
    def _existing_balance_assertion(
        workspace: str,
        assertion_date: str,
        account: str,
        currency: str,
        ledger_config: LedgerConfig | None = None,
    ) -> Decimal | None:
        """Find a checkpoint only in the active entry file's include graph."""
        config = _cfg(ledger_config)
        try:
            entries, _errors, _options = loader.load_file(_repo_path(workspace, config.entry_path))
        except OSError:
            return None
        for entry in entries:
            if (
                entry.__class__.__name__ == "Balance"
                and entry.date.isoformat() == assertion_date
                and entry.account == account
                and entry.amount.currency == currency
            ):
                return Decimal(entry.amount.number)
        return None

    def preview_balance_reconciliation(
        self,
        workspace: str,
        observed_date: str,
        account: str,
        amount: str,
        currency: str,
        adjustment_account: str,
        cutoff: str = "end_of_day",
        allow_existing_checkpoint: bool = False,
        include_assertion: bool = True,
        ledger_config: LedgerConfig | None = None,
    ) -> Preview | InvariantViolation | ValidationFailed:
        """Prepare an explicit adjustment transaction and verifiable assertion."""
        calculation = self.calculate_balance_adjustment(
            workspace, observed_date, account, amount, currency, cutoff, ledger_config
        )
        if not isinstance(calculation, QueryResult):
            return calculation
        if not _ACCOUNT_NAME_RE.match(adjustment_account):
            return InvariantViolation(
                invariant="RECONCILIATION_ADJUSTMENT_ACCOUNT",
                severity="HARD",
                provided=adjustment_account,
                remediation="Provide an existing explicit adjustment account.",
            )
        existing_accounts = set(LedgerQueryService.get_accounts(workspace, ledger_config))
        if adjustment_account not in existing_accounts:
            return InvariantViolation(
                invariant="RECONCILIATION_ADJUSTMENT_ACCOUNT",
                severity="HARD",
                provided=adjustment_account,
                remediation="The adjustment account must already be open in the ledger.",
            )
        details = calculation.rows[0]
        assertion_date = str(details["assertion_date"])
        checkpoint_amount = self._existing_balance_assertion(
            workspace, assertion_date, account, currency, ledger_config
        )
        if checkpoint_amount is not None and not allow_existing_checkpoint:
            return InvariantViolation(
                invariant="RECONCILIATION_CHECKPOINT_EXISTS",
                severity="HARD",
                provided={"account": account, "assertion_date": assertion_date},
                remediation=(
                    "Use ledger_prepare_balance_update to repair this existing checkpoint; "
                    "it will not be replaced automatically."
                ),
            )
        target_amount = Decimal(amount)
        current_amount = _parse_single_currency_balance(str(calculation.balance), currency)
        if isinstance(current_amount, InvariantViolation):
            return current_amount
        adjustment = target_amount - current_amount
        transaction_date = (date.fromisoformat(assertion_date) - timedelta(days=1)).isoformat()
        transaction_text = (
            f'{transaction_date} * "Balance reconciliation adjustment"\n'
            f"  {account}  {_format_decimal(adjustment)} {currency}\n"
            f"  {adjustment_account}  {_format_decimal(-adjustment)} {currency}"
        )
        assertion_text = (
            f"{assertion_date} balance {account}  {_format_decimal(target_amount)} {currency}"
        )
        generated_text = (
            f"{transaction_text}\n\n{assertion_text}" if include_assertion else transaction_text
        )

        plan = MutationPlanner.reconciliation(generated_text, "chore(ledger): reconcile balance")
        dry_run = _run_plan_validation(workspace, ledger_config, plan)
        if dry_run.failure:
            return dry_run.failure

        return Preview(
            proposal_id=f"prop_{uuid.uuid4().hex[:12]}",
            operation="balance_reconciliation",
            preview={
                "observed_date": observed_date,
                "cutoff": cutoff,
                "assertion_date": assertion_date,
                "account": account,
                "adjustment_account": adjustment_account,
                "currency": currency,
                "current_balance": str(calculation.balance),
                "target_balance": f"{_format_decimal(target_amount)} {currency}",
                "adjustment": f"{_format_decimal(adjustment)} {currency}",
                "assertion_status": "will_verify",
                "generated_text": generated_text,
                "target_file": dry_run.target_file,
                "validation": asdict(dry_run.validation),
            },
            message="Balance reconciliation passed dry-run validation. Request explicit approval.",
        )

    def prepare_balance_reconciliation(
        self,
        workspace: str,
        observed_date: str,
        account: str,
        amount: str,
        currency: str,
        adjustment_account: str = "",
        cutoff: str = "end_of_day",
        commit_message: str = "",
        ledger_config: LedgerConfig | None = None,
    ) -> PendingAction | InvariantViolation | ValidationFailed:
        preview = self.preview_balance_reconciliation(
            workspace,
            observed_date,
            account,
            amount,
            currency,
            adjustment_account,
            cutoff,
            False,
            True,
            ledger_config,
        )
        if not isinstance(preview, Preview):
            return preview
        details = preview.preview
        return PendingActionService.create_pending_action(
            action_type="balance_reconciliation",
            execution_spec={
                "mutation_plan": self._serialized_plan(
                    workspace,
                    MutationPlanner.reconciliation(str(details["generated_text"]), commit_message),
                    ledger_config,
                ),
                "observed_date": observed_date,
                "cutoff": cutoff,
                "account": account,
                "amount": amount,
                "currency": currency,
                "adjustment_account": adjustment_account,
                "is_checkpoint_update": False,
                "commit_message": commit_message or "chore(ledger): reconcile balance",
            },
            display={
                "kind": "balance_reconciliation_preview",
                "title": "Balance reconciliation",
                "summary": ("Prepare an explicit adjustment transaction and balance assertion"),
                "observed_date": observed_date,
                "cutoff": cutoff,
                "assertion_date": details["assertion_date"],
                "current_balance": details["current_balance"],
                "target_balance": details["target_balance"],
                "adjustment": details["adjustment"],
                "adjustment_account": adjustment_account,
                "assertion_status": details["assertion_status"],
                "warning": "Confirm that the unexplained difference is an intentional adjustment.",
                "generated_statements": details["generated_text"],
                "diff": details["generated_text"],
            },
            validation={
                "status": "validated",
                "account": account,
                "target_file": details["target_file"],
                "dry_run": details["validation"],
            },
        )

    def prepare_balance_update(
        self,
        workspace: str,
        assertion_date: str,
        account: str,
        currency: str,
        adjustment_account: str,
        commit_message: str = "",
        ledger_config: LedgerConfig | None = None,
    ) -> PendingAction | InvariantViolation | ValidationFailed:
        checkpoint_amount = self._existing_balance_assertion(
            workspace, assertion_date, account, currency, ledger_config
        )
        if checkpoint_amount is None:
            return InvariantViolation(
                invariant="RECONCILIATION_CHECKPOINT_NOT_FOUND",
                severity="HARD",
                provided={"account": account, "assertion_date": assertion_date},
                remediation=(
                    "Provide the account, currency, and assertion date of an "
                    "existing balance checkpoint."
                ),
            )
        observed_date = (date.fromisoformat(assertion_date) - timedelta(days=1)).isoformat()
        preview = self.preview_balance_reconciliation(
            workspace,
            observed_date,
            account,
            _format_decimal(checkpoint_amount),
            currency,
            adjustment_account,
            "end_of_day",
            True,
            False,
            ledger_config,
        )
        if not isinstance(preview, Preview):
            return preview
        details = preview.preview
        return PendingActionService.create_pending_action(
            action_type="balance_reconciliation",
            execution_spec={
                "mutation_plan": self._serialized_plan(
                    workspace,
                    MutationPlanner.reconciliation(
                        str(details["generated_text"]), commit_message, checkpoint_update=True
                    ),
                    ledger_config,
                ),
                "observed_date": observed_date,
                "cutoff": "end_of_day",
                "account": account,
                "amount": _format_decimal(checkpoint_amount),
                "currency": currency,
                "adjustment_account": adjustment_account,
                "is_checkpoint_update": True,
                "commit_message": commit_message or "chore(ledger): update balance checkpoint",
            },
            display={
                "kind": "balance_reconciliation_preview",
                "title": "Balance checkpoint update",
                "summary": (
                    "Prepare an explicit adjustment that restores an existing balance checkpoint."
                ),
                "observed_date": observed_date,
                "cutoff": "end_of_day",
                "assertion_date": assertion_date,
                "current_balance": details["current_balance"],
                "target_balance": details["target_balance"],
                "adjustment": details["adjustment"],
                "adjustment_account": adjustment_account,
                "assertion_status": details["assertion_status"],
                "warning": (
                    "This adds a new adjustment; it does not rewrite the earlier "
                    "transaction or assertion."
                ),
                "generated_statements": details["generated_text"],
                "diff": details["generated_text"],
            },
            validation={
                "status": "validated",
                "account": account,
                "target_file": details["target_file"],
                "dry_run": details["validation"],
            },
        )

    def confirm_balance_reconciliation(
        self,
        workspace: str,
        observed_date: str,
        account: str,
        amount: str,
        currency: str,
        repo_url: str,
        git_service: GitService,
        adjustment_account: str = "",
        cutoff: str = "end_of_day",
        is_checkpoint_update: bool = False,
        commit_message: str = "",
        github_token: str | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> CommitResult | ValidationFailed | DependencyUnavailable | InvariantViolation:
        preview = self.preview_balance_reconciliation(
            workspace,
            observed_date,
            account,
            amount,
            currency,
            adjustment_account,
            cutoff,
            is_checkpoint_update,
            not is_checkpoint_update,
            ledger_config,
        )
        if not isinstance(preview, Preview):
            return preview
        directives = str(preview.preview["generated_text"])
        plan = MutationPlanner.reconciliation(directives, commit_message)
        touched, git, failure = _apply_plan(
            workspace, ledger_config, plan, repo_url, git_service, github_token
        )
        if failure:
            return failure
        if dependency_error := _git_dependency_error(git):
            return dependency_error
        return CommitResult(
            outcome="Balance reconciliation validated and committed",
            result={
                "observed_date": observed_date,
                "target_file": touched[-1],
                "directives": directives,
            },
            push_status=git["push"],
        )

    # ═══════════════════════════════════════════════════════════════════════
    # Reconciliation query (coupled to mutation preparation)
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _get_reconciliation_balance(
        workspace: str,
        account: str,
        as_of_date: str,
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult:
        """Match Beancount balance directives by including descendant accounts."""
        account_pattern = re.escape(account)
        bql = (
            f"SELECT sum(position) AS balance "
            f'WHERE account ~ "^{account_pattern}(?::|$)" '
            f"AND date < {as_of_date}"
        )
        rows, error = Beancount.run_bql_rows(workspace, bql, ledger_config)
        if error:
            return QueryResult(status="ERROR", error=error)
        balance_raw = rows[0].get("balance", "").strip() if rows else ""
        return QueryResult(
            status="SUCCESS",
            account=account,
            as_of=as_of_date,
            balance=balance_raw if balance_raw else "0",
        )

    @staticmethod
    def preflight_report(
        workspace: str, ledger_config: LedgerConfig | None = None
    ) -> PreflightResult:
        return _read_only_preflight_report(workspace, ledger_config)
