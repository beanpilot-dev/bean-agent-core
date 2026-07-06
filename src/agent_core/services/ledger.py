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

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Callable

from .types import (
    ApplyReceipt,
    CommitResult,
    DependencyUnavailable,
    IntegrityFailed,
    InvariantViolation,
    LedgerConfig,
    LedgerMutationAction,
    PendingAction,
    PreflightResult,
    Preview,
    QueryResult,
    ValidationFailed,
    ValidationSummary,
)
from .workspace import GitService
from .beancount import Beancount, LedgerServiceError, _cfg, _repo_path

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

_PENDING_ACTION_SCHEMA_VERSION = 1
_PENDING_ACTION_TTL_MINUTES = 30

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

def _canonical_json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)

def _digest_payload(payload: dict) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

def _pending_action_digest_input(action: dict) -> dict:
    return {
        key: value
        for key, value in action.items()
        if key not in {"digest", "signature", "status", "message"}
    }

def _classify_action_risk(action_type: str, validation: dict[str, object]) -> dict[str, object]:
    reasons: list[str] = []
    risk = "normal"
    txn_count = validation.get("transaction_count")
    if (
        action_type in {"bulk_commit", "change_set"}
        and isinstance(txn_count, int)
        and txn_count >= 25
    ):
        risk = "high"
        reasons.append("bulk_transaction_count")
    if action_type == "update_transaction":
        risk = "elevated"
        reasons.append("historical_update")
    return {
        "version": "risk-policy-v1",
        "risk": risk,
        "reasons": reasons,
        "requires_elevated_review": risk == "high",
    }
# ---------------------------------------------------------------------------
# Sidecar helpers
# ---------------------------------------------------------------------------

def _agent_sidecar_target_file(ledger_config: LedgerConfig | None = None) -> str:
    config = _cfg(ledger_config)
    today = date.today()
    chunk_name = f"{today.year}-{today.month:02d}.beancount"
    return f"{config.sidecar_write_dir}/{chunk_name}"

def _check_sidecar_include(
    workspace: str, ledger_config: LedgerConfig | None = None
) -> bool:
    config = _cfg(ledger_config)
    main = _repo_path(workspace, config.entry_path)
    include = _include_line(config.entry_path, config.sidecar_main_path)
    try:
        with open(main) as f:
            return include in f.read()
    except OSError:
        return False

def _ensure_agent_sidecar(
    workspace: str, ledger_config: LedgerConfig | None = None
) -> str:
    config = _cfg(ledger_config)
    today = date.today()
    chunk_name = f"{today.year}-{today.month:02d}.beancount"
    agent_dir = _repo_path(workspace, config.sidecar_write_dir)
    os.makedirs(agent_dir, exist_ok=True)

    chunk_path = os.path.join(agent_dir, chunk_name)
    changed = False
    if not os.path.exists(chunk_path):
        with open(chunk_path, "w") as f:
            f.write(
                f"; Agent-generated transactions — {today.year}-{today.month:02d}\n"
            )
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
    def _make_pending_action(
        *,
        action_type: str,
        execution_spec: dict[str, object],
        display: dict[str, object],
        validation: dict[str, object],
    ) -> PendingAction:
        mutation = LedgerMutationAction(
            action_type=action_type,
            schema_version=_PENDING_ACTION_SCHEMA_VERSION,
            execution_spec=execution_spec,
            display=display,
            validation=validation,
        )
        pending_action_id = f"pa_{uuid.uuid4().hex[:16]}"
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=_PENDING_ACTION_TTL_MINUTES)
        ).isoformat()
        idempotency_key = _digest_payload({
            "action_type": mutation.action_type,
            "execution_spec": mutation.execution_spec,
            "validation": mutation.validation,
        })
        payload = {
            "pending_action_id": pending_action_id,
            "action_type": mutation.action_type,
            "schema_version": mutation.schema_version,
            "execution_spec": mutation.execution_spec,
            "display": mutation.display,
            "validation": mutation.validation,
            "policy": {
                "version": "pending-action-v1",
                "requires_approval": True,
                **_classify_action_risk(mutation.action_type, mutation.validation),
            },
            "expires_at": expires_at,
            "idempotency_key": idempotency_key,
            "continue_after_approval": False,
            "continuation_reason": "",
            "next_intent_summary": "",
        }
        digest = _digest_payload(payload)
        return PendingAction(
            pending_action_id=pending_action_id,
            action_type=action_type,
            schema_version=_PENDING_ACTION_SCHEMA_VERSION,
            execution_spec=execution_spec,
            display=display,
            validation=validation,
            policy=payload["policy"],
            expires_at=expires_at,
            idempotency_key=idempotency_key,
            continue_after_approval=False,
            continuation_reason="",
            next_intent_summary="",
            digest=digest,
            signature=f"sha256:{digest}",
            message="Prepared action is awaiting explicit user approval.",
        )

    @staticmethod
    def verify_pending_action(action: dict[str, object]) -> IntegrityFailed | None:
        pending_action_id = str(action.get("pending_action_id") or "")
        digest = action.get("digest")
        signature = action.get("signature")
        if not isinstance(digest, str) or not digest:
            return IntegrityFailed(
                pending_action_id=pending_action_id,
                error="Missing pending action digest.",
            )
        expected = _digest_payload(_pending_action_digest_input(action))
        if digest != expected or signature != f"sha256:{expected}":
            return IntegrityFailed(
                pending_action_id=pending_action_id,
                error="Pending action integrity check failed.",
            )
        expires_at = action.get("expires_at")
        if isinstance(expires_at, str) and expires_at:
            try:
                expires = datetime.fromisoformat(expires_at)
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires <= datetime.now(timezone.utc):
                    return IntegrityFailed(
                        pending_action_id=pending_action_id,
                        error="Pending action has expired.",
                    )
            except ValueError:
                return IntegrityFailed(
                    pending_action_id=pending_action_id,
                    error="Pending action expiry is invalid.",
                )
        return None

    @staticmethod
    def _extract_accounts(transaction_text: str) -> list[str]:
        return sorted({
            m.group(0).strip()
            for m in _POSTING_ACCOUNT_RE.finditer(transaction_text)
        })

    @staticmethod
    def get_accounts(
        workspace: str, ledger_config: LedgerConfig | None = None
    ) -> list[str]:
        rows, err = Beancount.run_bql_rows(
            workspace, "SELECT DISTINCT account ORDER BY account", ledger_config
        )
        if err:
            raise LedgerServiceError(f"Failed to list accounts: {err}")
        accounts = {r["account"] for r in rows if r.get("account")}
        try:
            for dirpath, dirnames, filenames in os.walk(workspace):
                dirnames[:] = [d for d in dirnames if d not in {".git", ".venv"}]
                for fname in filenames:
                    if not fname.endswith(".beancount"):
                        continue
                    with open(os.path.join(dirpath, fname), encoding="utf-8") as f:
                        accounts.update(_OPEN_ACCOUNT_RE.findall(f.read()))
        except OSError:
            pass
        return sorted(accounts)

    @staticmethod
    def validate_accounts(
        workspace: str,
        transaction_text: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> InvariantViolation | None:
        used = LedgerService._extract_accounts(transaction_text)
        valid = set(LedgerService.get_accounts(workspace, ledger_config))

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
        ledger_config: LedgerConfig | None = None,
    ) -> Preview | InvariantViolation | ValidationFailed:
        """Validate a transaction proposal in an isolated dry-run."""
        violation = self.validate_accounts(
            workspace, transaction_text, whitelist, ledger_config
        )
        if violation:
            return violation

        accounts = LedgerService._extract_accounts(transaction_text)
        dry_run = _run_isolated_validation(
            workspace,
            ledger_config,
            lambda dry_workspace: _append_to_sidecar(
                dry_workspace, transaction_text, ledger_config
            ),
            "Fix the transaction syntax or balancing and prepare it again.",
        )
        if dry_run.failure:
            return dry_run.failure

        target = dry_run.target_file or _agent_sidecar_target_file(ledger_config)
        pid = f"prop_{uuid.uuid4().hex[:12]}"

        return Preview(
            proposal_id=pid,
            operation="commit_transaction",
            preview={
                "transaction": transaction_text,
                "accounts_validated": accounts,
                "target_file": target,
                "commit_message": commit_message,
                "validation": asdict(dry_run.validation),
            },
            message=(
                "All accounts and dry-run validation passed. Show this preview "
                "to the user and request explicit approval."
            ),
        )

    def prepare_commit(
        self,
        workspace: str,
        transaction_text: str,
        commit_message: str,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> PendingAction | InvariantViolation | ValidationFailed:
        preview = self.preview_commit(
            workspace, transaction_text, commit_message, whitelist, ledger_config
        )
        if not isinstance(preview, Preview):
            return preview
        return self._make_pending_action(
            action_type="commit_transaction",
            execution_spec={
                "transaction_text": transaction_text,
                "commit_message": commit_message,
            },
            display={
                "kind": "transaction_preview",
                "summary": "Record a transaction",
                "diff": transaction_text,
                "preview": preview.preview,
            },
            validation={
                "status": "validated",
                "accounts": preview.preview.get("accounts_validated", []),
                "target_file": preview.preview.get("target_file"),
                "dry_run": preview.preview.get("validation"),
            },
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

        target = preview.preview["target_file"]
        _ensure_agent_sidecar(workspace, ledger_config)
        target_path = os.path.join(workspace, target)
        backup_path = target_path + ".bak"

        with open(target_path) as f:
            original = f.read()
        with open(backup_path, "w") as f:
            f.write(original)

        _append_to_sidecar(workspace, transaction_text, ledger_config)

        is_clean, check_output = Beancount.bean_check(workspace, ledger_config)
        if not is_clean:
            with open(target_path, "w") as f:
                f.write(original)
            Beancount.invalidate_cache(workspace, ledger_config)
            os.remove(backup_path)
            return _validation_failure(
                check_output,
                "Fix the transaction syntax or balancing and prepare it again.",
            )

        os.remove(backup_path)
        Beancount.bean_format(workspace, target_path)

        git = git_service.commit_and_push(
            workspace, commit_message, repo_url, github_token
        )
        if dependency_error := _git_dependency_error(git):
            return dependency_error

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
        ledger_config: LedgerConfig | None = None,
    ) -> Preview | InvariantViolation | ValidationFailed:
        """Validate an open-account proposal in an isolated dry-run."""
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

        existing = self.get_accounts(workspace, ledger_config)
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

        dry_run = _run_isolated_validation(
            workspace,
            ledger_config,
            lambda dry_workspace: _write_open_directive(
                dry_workspace, account_name, directive_text, ledger_config
            ),
            "Fix the account directive and prepare it again.",
        )
        if dry_run.failure:
            return dry_run.failure

        pid = f"prop_{uuid.uuid4().hex[:12]}"

        return Preview(
            proposal_id=pid,
            operation="open_account",
            preview={
                "directive": directive_text,
                "account": account_name,
                "currency": currency,
                "open_date": open_date,
                "target_file": dry_run.target_file,
                "validation": asdict(dry_run.validation),
            },
            message=(
                "Account directive passed dry-run validation. Request explicit approval."
            ),
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
        _ensure_agent_sidecar(workspace, config)
        main_path = _repo_path(workspace, config.sidecar_main_path)

        try:
            with open(main_path) as f:
                original = f.read()
        except OSError as e:
            return DependencyUnavailable(error=f"Cannot read main.beancount: {e}")

        _write_open_directive(workspace, account_name, directive_text, config)

        is_clean, check_output = Beancount.bean_check(workspace, config)
        if not is_clean:
            with open(main_path, "w") as f:
                f.write(original)
            Beancount.invalidate_cache(workspace, config)
            return _validation_failure(
                check_output,
                "Fix the account directive and prepare it again.",
            )

        Beancount.bean_format(workspace, main_path)
        git = git_service.commit_and_push(
            workspace,
            f"chore(accounts): open {account_name}",
            repo_url,
            github_token,
        )
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
        preview = self.preview_open(
            workspace, account_name, currency, open_date, display_name, ledger_config
        )
        if not isinstance(preview, Preview):
            return preview
        return self._make_pending_action(
            action_type="open_account",
            execution_spec={
                "account_name": account_name,
                "currency": currency,
                "open_date": open_date,
                "display_name": display_name,
            },
            display={
                "kind": "account_open_preview",
                "summary": "Open an account",
                "diff": preview.preview.get("directive", ""),
                "preview": preview.preview,
            },
            validation={
                "status": "validated",
                "account": account_name,
                "dry_run": preview.preview.get("validation"),
            },
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
            f'SELECT DISTINCT date, narration '
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
                        raw = (
                            rest[:end_match.start()].rstrip()
                            if end_match else rest.rstrip()
                        )
                        results.append((rel, content, raw))
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
        ledger_config: LedgerConfig | None = None,
    ) -> Preview | InvariantViolation | ValidationFailed:
        """Find and validate a replacement transaction in an isolated dry-run."""
        matches = self.find_transaction_block(
            workspace, target_date, narration, ledger_config
        )

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
            workspace, new_transaction_text, whitelist, ledger_config,
        )
        if violation:
            return violation

        advisory = self._detect_value_change(old_block, new_transaction_text)
        dry_run = _run_isolated_validation(
            workspace,
            ledger_config,
            lambda dry_workspace: _replace_transaction_block(
                dry_workspace,
                rel_path,
                old_block,
                new_transaction_text,
                ledger_config,
            ),
            "bean-check failed after replacement. Adjust the transaction and prepare it again.",
        )
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
            message=(
                "Replacement passed dry-run validation. Request explicit approval."
            ),
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
            workspace, target_date, narration, new_transaction_text,
            commit_message, whitelist, ledger_config,
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

        _replace_transaction_block(
            workspace, rel_path, old_block, new_transaction_text, ledger_config
        )

        is_clean, check_output = Beancount.bean_check(workspace, ledger_config)
        if not is_clean:
            with open(file_path, "w") as f:
                f.write(original)
            Beancount.invalidate_cache(workspace, ledger_config)
            os.remove(backup_path)
            return _validation_failure(
                check_output,
                "bean-check failed after replacement. Adjust the transaction and prepare it again.",
            )

        os.remove(backup_path)
        Beancount.bean_format(workspace, file_path)

        git = git_service.commit_and_push(
            workspace, commit_message, repo_url, github_token
        )
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
            workspace, target_date, narration, new_transaction_text,
            commit_message, whitelist, ledger_config,
        )
        if not isinstance(preview, Preview):
            return preview
        return self._make_pending_action(
            action_type="update_transaction",
            execution_spec={
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
            workspace, transactions_text, whitelist, ledger_config,
        )
        if violation:
            return violation

        dry_run = _run_isolated_validation(
            workspace,
            ledger_config,
            lambda dry_workspace: _append_to_sidecar(
                dry_workspace, transactions_text, ledger_config
            ),
            "bean-check failed. Revise the transaction batch and prepare it again.",
        )
        if dry_run.failure:
            return dry_run.failure

        target = dry_run.target_file or _agent_sidecar_target_file(ledger_config)

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
            workspace, transactions_text, commit_message,
            None, whitelist, ledger_config,
        )
        if not isinstance(preview, Preview):
            return preview

        target = preview.preview["target_file"]
        txn_count = preview.preview["transaction_count"]

        _ensure_agent_sidecar(workspace, ledger_config)
        target_path = os.path.join(workspace, target)
        backup_path = target_path + ".bak"

        with open(target_path) as f:
            original = f.read()
        with open(backup_path, "w") as f:
            f.write(original)

        _append_to_sidecar(workspace, transactions_text, ledger_config)

        is_clean, check_output = Beancount.bean_check(workspace, ledger_config)
        if not is_clean:
            with open(target_path, "w") as f:
                f.write(original)
            Beancount.invalidate_cache(workspace, ledger_config)
            os.remove(backup_path)
            return _validation_failure(
                check_output,
                "bean-check failed. Revise the transaction batch and prepare it again.",
            )

        os.remove(backup_path)
        Beancount.bean_format(workspace, target_path)

        if git_service is None:
            return DependencyUnavailable(error="Git service is not configured")
        git = git_service.commit_and_push(
            workspace, commit_message, repo_url, github_token
        )
        if dependency_error := _git_dependency_error(git):
            return dependency_error

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
        return self._make_pending_action(
            action_type="bulk_commit",
            execution_spec={
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
                existing = self.get_accounts(workspace, ledger_config)
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
                display_items.append({
                    "operation_index": index,
                    "type": "open_account",
                    "summary": f"Open {account_name}",
                    "diff": directive_text,
                    "target_file": target,
                })
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
                display_items.append({
                    "operation_index": index,
                    "type": "commit_transaction",
                    "summary": "Record a transaction",
                    "diff": transaction_text,
                    "target_file": target,
                    "accounts": accounts,
                })
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
        config = _cfg(ledger_config)
        try:
            with tempfile.TemporaryDirectory(prefix="beanpilot-change-set-") as tmp:
                dry_workspace = os.path.join(tmp, "workspace")
                _copy_workspace_for_dry_run(workspace, dry_workspace)
                try:
                    replay = self._replay_change_set_operations(
                        dry_workspace, operations, whitelist, config
                    )
                    if isinstance(replay, InvariantViolation):
                        return replay
                    Beancount.invalidate_cache(dry_workspace, config)
                    is_clean, check_output = Beancount.bean_check(dry_workspace, config)
                    if not is_clean:
                        return _validation_failure(
                            check_output,
                            "Fix the change-set operations and prepare them again.",
                        )
                finally:
                    Beancount.invalidate_workspace(dry_workspace)
        except OSError as exc:
            raise LedgerServiceError("Change-set dry-run workspace unavailable") from exc

        diff = "\n\n".join(str(item.get("diff") or "") for item in replay.display_items)
        return self._make_pending_action(
            action_type="change_set",
            execution_spec={
                "operations": operations,
                "commit_message": commit_message,
            },
            display={
                "kind": "change_set_preview",
                "summary": f"Apply {len(operations)} related ledger changes",
                "diff": diff,
                "items": replay.display_items,
            },
            validation={
                "status": "validated",
                "operation_count": len(operations),
                "transaction_count": replay.transaction_count,
                "accounts": replay.affected_accounts,
                "target_files": replay.touched_files,
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

        config = _cfg(ledger_config)
        try:
            with tempfile.TemporaryDirectory(prefix="beanpilot-change-set-apply-") as tmp:
                apply_workspace = os.path.join(tmp, "workspace")
                _copy_workspace_for_dry_run(workspace, apply_workspace)
                try:
                    replay = self._replay_change_set_operations(
                        apply_workspace, operations, whitelist, config
                    )
                    if isinstance(replay, InvariantViolation):
                        return replay
                    Beancount.invalidate_cache(apply_workspace, config)
                    is_clean, check_output = Beancount.bean_check(apply_workspace, config)
                    if not is_clean:
                        return _validation_failure(
                            check_output,
                            "Fix the change-set operations and prepare them again.",
                        )
                    for rel_path in replay.touched_files:
                        Beancount.bean_format(
                            apply_workspace, _repo_path(apply_workspace, rel_path)
                        )

                    originals = {
                        rel_path: _read_repo_file(workspace, rel_path)
                        for rel_path in replay.touched_files
                    }
                    try:
                        for rel_path in replay.touched_files:
                            _write_repo_file(
                                workspace,
                                rel_path,
                                _read_repo_file(apply_workspace, rel_path),
                            )
                        Beancount.invalidate_workspace(workspace)
                    except OSError:
                        _restore_repo_files(workspace, originals)
                        raise
                finally:
                    Beancount.invalidate_workspace(apply_workspace)
        except OSError as exc:
            raise LedgerServiceError("Change-set apply workspace unavailable") from exc

        try:
            git = git_service.commit_and_push(
                workspace, commit_message, repo_url, github_token
            )
        except Exception:
            _restore_repo_files(workspace, originals)
            raise
        if dependency_error := _git_dependency_error(git):
            _restore_repo_files(workspace, originals)
            return dependency_error

        return CommitResult(
            outcome=f"{len(operations)} ledger changes validated and committed",
            result={
                "operation_count": len(operations),
                "transaction_count": replay.transaction_count,
                "target_files": replay.touched_files,
            },
            push_status=git["push"],
        )

    def apply_pending_action(
        self,
        workspace: str,
        action: dict[str, object],
        repo_url: str,
        git_service: GitService,
        github_token: str | None = None,
        whitelist: list[str] | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> (
        ApplyReceipt
        | CommitResult
        | ValidationFailed
        | DependencyUnavailable
        | InvariantViolation
        | IntegrityFailed
    ):
        integrity = self.verify_pending_action(action)
        if integrity:
            return integrity

        action_type = str(action.get("action_type") or "")
        spec = action.get("execution_spec")
        if not isinstance(spec, dict):
            return IntegrityFailed(
                pending_action_id=str(action.get("pending_action_id") or ""),
                error="Pending action execution spec is invalid.",
            )

        if action_type == "commit_transaction":
            result = self.confirm_commit(
                workspace,
                str(spec.get("transaction_text") or ""),
                str(spec.get("commit_message") or ""),
                repo_url,
                git_service,
                github_token,
                whitelist,
                ledger_config,
            )
        elif action_type == "open_account":
            result = self.confirm_open(
                workspace,
                str(spec.get("account_name") or ""),
                spec.get("currency") if isinstance(spec.get("currency"), str) else None,
                str(spec.get("open_date") or ""),
                repo_url,
                git_service,
                spec.get("display_name") if isinstance(spec.get("display_name"), str) else None,
                github_token,
                ledger_config,
            )
        elif action_type == "update_transaction":
            result = self.confirm_update(
                workspace,
                str(spec.get("target_date") or ""),
                str(spec.get("narration") or ""),
                str(spec.get("new_transaction_text") or ""),
                str(spec.get("commit_message") or ""),
                repo_url,
                git_service,
                github_token,
                whitelist,
                ledger_config,
            )
        elif action_type == "bulk_commit":
            result = self.confirm_bulk(
                workspace,
                str(spec.get("transactions_text") or ""),
                str(spec.get("commit_message") or ""),
                repo_url,
                git_service,
                None,
                github_token,
                whitelist,
                ledger_config,
            )
        elif action_type == "change_set":
            raw_operations = spec.get("operations")
            if not isinstance(raw_operations, list) or not all(
                isinstance(operation, dict) for operation in raw_operations
            ):
                return IntegrityFailed(
                    pending_action_id=str(action.get("pending_action_id") or ""),
                    error="Change-set operations are invalid.",
                )
            result = self.confirm_change_set(
                workspace,
                raw_operations,
                str(spec.get("commit_message") or ""),
                repo_url,
                git_service,
                github_token,
                whitelist,
                ledger_config,
            )
        else:
            return IntegrityFailed(
                pending_action_id=str(action.get("pending_action_id") or ""),
                error=f"Unsupported pending action type: {action_type}",
            )

        if isinstance(result, CommitResult):
            return ApplyReceipt(
                pending_action_id=str(action.get("pending_action_id") or ""),
                action_type=action_type,
                receipt=asdict(result),
            )
        return result

    # ═══════════════════════════════════════════════════════════════════════
    # Read operations (stateless)
    # ═══════════════════════════════════════════════════════════════════════

    @staticmethod
    def get_balance(
        workspace: str,
        account: str,
        as_of_date: str | None = None,
        ledger_config: LedgerConfig | None = None,
    ) -> QueryResult:
        date_clause = f'AND date < "{as_of_date}"' if as_of_date else ""
        bql = (
            f'SELECT sum(position) AS balance '
            f'WHERE account ~ "^{account}$" {date_clause}'
        )
        rows, error = Beancount.run_bql_rows(workspace, bql, ledger_config)
        if error:
            return QueryResult(
                status="ERROR", error=error,
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
        ledger_config: LedgerConfig | None = None,
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

        rows, error = Beancount.run_bql_rows(workspace, bql, ledger_config)
        if error:
            return QueryResult(status="ERROR", error=error)

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
    def query_bql(
        workspace: str, bql: str, ledger_config: LedgerConfig | None = None
    ) -> QueryResult:
        rows, error = Beancount.run_bql_rows(workspace, bql, ledger_config)
        if error:
            return QueryResult(status="ERROR", error=error, bql=bql)
        return QueryResult(status="SUCCESS", count=len(rows), rows=rows)

    @staticmethod
    def query_template(
        workspace: str,
        template_name: str,
        params: dict,
        templates_dir: str | None = None,
        ledger_config: LedgerConfig | None = None,
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
                    line for line in f if not line.lstrip().startswith("--")
                ]
            bql = "".join(lines).strip()
        except FileNotFoundError:
            return QueryResult(
                status="ERROR", error=f"Template file not found: {template_name}",
            )

        for key, value in params.items():
            bql = bql.replace(f"{{{key}}}", str(value))

        rows, error = Beancount.run_bql_rows(workspace, bql, ledger_config)
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
    def preflight_report(
        workspace: str, ledger_config: LedgerConfig | None = None
    ) -> PreflightResult:
        config = _cfg(ledger_config)
        if not _check_sidecar_include(workspace, config):
            sidecar_include = _include_line(
                config.entry_path, config.sidecar_main_path
            )[9:-1]
            return PreflightResult(
                status="SETUP_REQUIRED",
                action=(
                    f'Add include "{sidecar_include}" to {config.entry_path}'
                ),
            )

        target = _ensure_agent_sidecar(workspace, config)
        is_clean, check_output = Beancount.bean_check(workspace, config)
        accounts = LedgerService.get_accounts(workspace, config)

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
