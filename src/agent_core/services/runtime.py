# ruff: noqa: E501
"""Private, deterministic ledger-runtime protocol.

This module is intentionally independent from LangGraph and model providers.
The public MCP adapter can dispatch bounded domain commands here without
turning the execution plane into an agent host.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .approvals.contracts import PendingActionService
from .beancount import Beancount
from .ledger_paths import sidecar_target_file
from .mutations.coordinator import MutationCoordinator
from .mutations.handlers.contracts import PreparedMutation
from .mutations.handlers.registry import MutationPreparationHandlerRegistry
from .mutations.preparation import MutationPreparationService
from .mutations.executor import MutationExecutor
from .approvals.contracts import digest_payload
from .mutations.validator import PlanValidation
from .operations.lifecycle import (
    PreflightMode,
    RequestWorkspaceLifecycle,
    WorkspaceCacheBusyError,
    WorkspaceGitError,
    WorkspacePluginError,
    WorkspaceSetupRequiredError,
)
from .queries import LedgerQueryService
from .types import InvariantViolation, LedgerConfig, ValidationFailed
from .workspace import GitService

MAX_ARGUMENT_BYTES = 64_000
MAX_OPERATIONS = 25
MAX_QUERY_LIMIT = 100
MAX_REPORT_LIMIT = 100
REPORT_KINDS = {
    "account_balance",
    "trial_balance",
    "income_statement",
    "cash_flow",
    "account_activity",
}
STRUCTURED_OPERATION_KEYS = {
    "create_transaction",
    "update_transaction",
    "delete_transaction",
    "open_account",
    "close_account",
    "set_price",
    "set_balance_checkpoint",
}


class RuntimeProtocolError(ValueError):
    """A bounded private-runtime request is malformed or not allowed."""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class RuntimeRepo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2_000)
    token: str = Field(min_length=1, max_length=4_096)


class RuntimeLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_path: str = Field(default="data/main.beancount", max_length=300)
    sidecar_main_path: str | None = Field(default=None, max_length=300)
    sidecar_write_dir: str = Field(default="data/agent_inc", max_length=300)


class RuntimeExecuteRequest(BaseModel):
    """Versioned private execution request; never exposed as an MCP schema."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["ledger.v1"] = "ledger.v1"
    execution_id: str = Field(min_length=1, max_length=128)
    operation: Literal[
        "preflight",
        "search_accounts",
        "search_transactions",
        "get_transaction",
        "run_report",
        "prepare_change_set",
        "apply_sealed_plan",
        "inspect_publication_marker",
    ]
    repo: RuntimeRepo
    user_id: str = Field(min_length=1, max_length=200)
    request_id: str | None = Field(default=None, max_length=128)
    branch: str = Field(min_length=1, max_length=255)
    ledger: RuntimeLedger = RuntimeLedger()
    arguments: dict[str, Any] = Field(default_factory=dict)
    sealed_action: dict[str, Any] | None = None
    expected_head_sha: str | None = Field(default=None, max_length=128)


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_branch(branch: str) -> str:
    try:
        return GitService._validate_branch(branch)
    except ValueError as exc:
        raise RuntimeProtocolError("INVALID_BRANCH", "branch is not a valid branch name") from exc


def _config(payload: RuntimeLedger) -> LedgerConfig:
    try:
        return LedgerConfig(
            entry_path=payload.entry_path,
            sidecar_main_path=payload.sidecar_main_path,
            sidecar_write_dir=payload.sidecar_write_dir,
        )
    except ValueError as exc:
        raise RuntimeProtocolError(
            "LEDGER_CONFIG_INVALID", "ledger configuration is invalid"
        ) from exc


def _argument_keys(arguments: dict[str, Any], allowed: set[str]) -> None:
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise RuntimeProtocolError(
            "INVALID_ARGUMENTS",
            "arguments contain unsupported fields",
            details={"fields": unknown},
        )


def _require_text(arguments: dict[str, Any], key: str, max_length: int = 500) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise RuntimeProtocolError("INVALID_ARGUMENTS", f"{key} must be a bounded non-empty string")
    return value.strip()


def _optional_text(arguments: dict[str, Any], key: str, max_length: int = 500) -> str | None:
    value = arguments.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > max_length:
        raise RuntimeProtocolError("INVALID_ARGUMENTS", f"{key} must be a bounded string")
    return value.strip()


def _single_line(value: object, key: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise RuntimeProtocolError("INVALID_ARGUMENTS", f"{key} must be a bounded string")
    if any(ord(char) < 32 for char in value):
        raise RuntimeProtocolError("INVALID_ARGUMENTS", f"{key} must be a single-line string")
    return value.strip()


def _iso_date(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise RuntimeProtocolError("INVALID_ARGUMENTS", f"{key} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeProtocolError("INVALID_ARGUMENTS", f"{key} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise RuntimeProtocolError("INVALID_ARGUMENTS", f"{key} must be an ISO date")
    return value


def _bounded_limit(arguments: dict[str, Any], maximum: int = MAX_QUERY_LIMIT) -> int:
    value = arguments.get("limit", 20)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > maximum:
        raise RuntimeProtocolError("INVALID_ARGUMENTS", "limit is outside the supported bound")
    return value


def _render_units(value: object) -> str:
    if not isinstance(value, dict) or set(value) != {"number", "currency"}:
        raise RuntimeProtocolError("INVALID_ARGUMENTS", "units must contain number and currency")
    number = value.get("number")
    currency = value.get("currency")
    if not isinstance(number, str) or not re.fullmatch(r"[-+]?\d+(?:\.\d+)?", number):
        raise RuntimeProtocolError("INVALID_ARGUMENTS", "units.number must be a decimal string")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z][A-Z0-9\-]{0,14}", currency):
        raise RuntimeProtocolError("INVALID_ARGUMENTS", "units.currency is invalid")
    try:
        Decimal(number)
    except InvalidOperation as exc:
        raise RuntimeProtocolError("INVALID_ARGUMENTS", "units.number is invalid") from exc
    return f"{number} {currency}"


def _render_transaction(value: object) -> str:
    if not isinstance(value, dict):
        raise RuntimeProtocolError("INVALID_ARGUMENTS", "transaction must be an object")
    allowed = {"date", "flag", "payee", "narration", "postings", "tags", "links", "meta"}
    _argument_keys(value, allowed)
    transaction_date = _iso_date(value.get("date"), "transaction.date")
    flag = value.get("flag", "*")
    if not isinstance(flag, str) or not re.fullmatch(r"[*!PQRSTUC]", flag):
        raise RuntimeProtocolError("INVALID_ARGUMENTS", "transaction.flag is invalid")
    payee = value.get("payee")
    narration = value.get("narration")
    if payee is not None:
        payee = _single_line(payee, "transaction.payee", 200)
    if narration is not None:
        narration = _single_line(narration, "transaction.narration", 500)
    if payee is None and narration is None:
        raise RuntimeProtocolError("INVALID_ARGUMENTS", "transaction needs payee or narration")
    postings = value.get("postings")
    if not isinstance(postings, list) or not 2 <= len(postings) <= 20:
        raise RuntimeProtocolError("INVALID_ARGUMENTS", "transaction.postings count is invalid")
    lines = [f'{transaction_date} {flag} "{(payee or narration).replace(chr(34), chr(39))}"']
    if payee is not None and narration is not None:
        lines[0] = (
            f'{transaction_date} {flag} "{payee.replace(chr(34), chr(39))}" "{narration.replace(chr(34), chr(39))}"'
        )
    for posting in postings:
        if not isinstance(posting, dict):
            raise RuntimeProtocolError("INVALID_ARGUMENTS", "posting must be an object")
        _argument_keys(posting, {"account", "units"})
        account = posting.get("account")
        if not isinstance(account, str) or not re.fullmatch(
            r"(?:Assets|Liabilities|Equity|Income|Expenses)(?::[A-Za-z][A-Za-z0-9\-]+)+", account
        ):
            raise RuntimeProtocolError("INVALID_ARGUMENTS", "posting.account is invalid")
        units = _render_units(posting.get("units")) if posting.get("units") is not None else ""
        if posting.get("units") is None:
            raise RuntimeProtocolError("INVALID_ARGUMENTS", "posting.units is required")
        lines.append(f"  {account}  {units}")
    tags = value.get("tags", [])
    links = value.get("links", [])
    if (
        not isinstance(tags, list)
        or not isinstance(links, list)
        or len(tags) > 20
        or len(links) > 20
    ):
        raise RuntimeProtocolError(
            "INVALID_ARGUMENTS", "transaction tags and links are bounded arrays"
        )
    for tag in tags:
        if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9_\-/]{1,80}", tag):
            raise RuntimeProtocolError("INVALID_ARGUMENTS", "transaction tag is invalid")
        lines[0] += f" #{tag}"
    for link in links:
        if not isinstance(link, str) or not re.fullmatch(r"[A-Za-z0-9_\-/]{1,80}", link):
            raise RuntimeProtocolError("INVALID_ARGUMENTS", "transaction link is invalid")
        lines[0] += f" ^{link}"
    if value.get("meta") not in (None, {}):
        raise RuntimeProtocolError("INVALID_ARGUMENTS", "transaction metadata is not supported yet")
    return "\n".join(lines)


class PrivateLedgerRuntime:
    """Execute the private protocol on one isolated, branch-bound workspace."""

    def __init__(self, lifecycle: RequestWorkspaceLifecycle):
        self._lifecycle = lifecycle
        self._preparation = MutationPreparationService()
        self._registry = MutationPreparationHandlerRegistry()

    async def execute(self, request: RuntimeExecuteRequest) -> dict[str, Any]:
        if len(json.dumps(request.arguments, ensure_ascii=False, default=str)) > MAX_ARGUMENT_BYTES:
            raise RuntimeProtocolError("INPUT_TOO_LARGE", "arguments exceed the request limit")
        branch = _safe_branch(request.branch)
        config = _config(request.ledger)
        if request.operation == "apply_sealed_plan":
            try:
                return self._apply_sealed(request, branch, config)
            except WorkspacePluginError as exc:
                return _runtime_error("PLUGIN_REJECTED", str(exc))
            except WorkspaceSetupRequiredError:
                return _runtime_error("LEDGER_SETUP_REQUIRED", "ledger sidecar setup is incomplete")
            except WorkspaceCacheBusyError:
                return _runtime_error("RUNTIME_BUSY", "ledger runtime workspace is busy", retryable=True)
            except WorkspaceGitError as exc:
                return _runtime_error(exc.code, "repository is unavailable", retryable=True)
        try:
            with self._lifecycle.open(
                repo_url=request.repo.url,
                token=request.repo.token,
                user_id=request.user_id,
                branch=branch,
                prefix="bean_runtime_",
                preflight_mode=PreflightMode.VALIDATE,
                ledger_config=config,
            ) as prepared:
                head_sha = _head_sha(prepared.path)
                preflight = prepared.preflight
                if request.operation == "preflight":
                    return self._status(preflight, head_sha, branch, config)
                if preflight is None or preflight.status != "CLEAN":
                    return _runtime_error(
                        "LEDGER_INVALID",
                        "ledger preflight did not pass",
                        details={"preflight": asdict(preflight) if preflight else {}},
                    )
                result = self._domain(
                    request.operation,
                    request.arguments,
                    prepared.path,
                    config,
                    owner=request.user_id,
                    branch=branch,
                    head_sha=head_sha,
                )
                result["head_sha"] = head_sha
                result["branch"] = branch
                return result
        except WorkspacePluginError as exc:
            return _runtime_error("PLUGIN_REJECTED", str(exc))
        except WorkspaceSetupRequiredError:
            return _runtime_error("LEDGER_SETUP_REQUIRED", "ledger sidecar setup is incomplete")
        except WorkspaceCacheBusyError:
            return _runtime_error(
                "RUNTIME_BUSY", "ledger runtime workspace is busy", retryable=True
            )
        except WorkspaceGitError as exc:
            return _runtime_error(exc.code, "repository is unavailable", retryable=True)

    @staticmethod
    def _status(preflight: Any, head_sha: str, branch: str, config: LedgerConfig) -> dict[str, Any]:
        return {
            "status": "ok" if preflight and preflight.status == "CLEAN" else "error",
            "branch": branch,
            "head_sha": head_sha,
            "ledger": asdict(config),
            "preflight": asdict(preflight) if preflight else None,
        }

    def _domain(
        self,
        operation: str,
        arguments: dict[str, Any],
        workspace: str,
        config: LedgerConfig,
        *,
        owner: str = "",
        branch: str = "",
        head_sha: str = "",
    ) -> dict[str, Any]:
        if operation == "search_accounts":
            _argument_keys(arguments, {"query", "account_type", "status", "limit"})
            result = LedgerQueryService.find_accounts(
                workspace,
                _require_text(arguments, "query", 200),
                str(arguments.get("account_type") or ""),
                str(arguments.get("status") or "open"),
                _bounded_limit(arguments),
                ledger_config=config,
            )
            return {
                "status": "ok" if result.status == "SUCCESS" else "error",
                "result": asdict(result),
            }
        if operation == "search_transactions":
            _argument_keys(
                arguments, {"account", "date_from", "date_to", "narration_contains", "limit"}
            )
            result = LedgerQueryService.find_transactions(
                workspace,
                account=_optional_text(arguments, "account", 300),
                date_from=_iso_date(arguments["date_from"], "date_from")
                if arguments.get("date_from")
                else None,
                date_to=_iso_date(arguments["date_to"], "date_to")
                if arguments.get("date_to")
                else None,
                narration_contains=_optional_text(arguments, "narration_contains", 300),
                limit=_bounded_limit(arguments),
                ledger_config=config,
            )
            return {
                "status": "ok" if result.status == "SUCCESS" else "error",
                "result": asdict(result),
            }
        if operation == "get_transaction":
            _argument_keys(arguments, {"transaction_ref"})
            result = LedgerQueryService.get_transaction(
                workspace, _require_text(arguments, "transaction_ref", 1_000), config
            )
            return {
                "status": "ok" if result.status == "SUCCESS" else "error",
                "result": asdict(result),
            }
        if operation == "run_report":
            return self._report(arguments, workspace, config)
        if operation == "prepare_change_set":
            return self._prepare(
                arguments,
                workspace,
                config,
                owner=owner,
                branch=branch,
                head_sha=head_sha,
            )
        raise RuntimeProtocolError(
            "UNSUPPORTED_OPERATION", "operation is not available in this runtime"
        )

    def _apply_sealed(
        self, request: RuntimeExecuteRequest, branch: str, config: LedgerConfig
    ) -> dict[str, Any]:
        """Replay one authenticated, browser-approved sealed mutation plan.

        The control plane has already performed owner, proposal, and approval
        checks.  The runtime still verifies the sealed action digest, branch,
        base revision, and the plan's file preconditions before it gets a
        publisher capability.  Git's fast-forward push is the final remote
        compare-and-swap: a concurrent branch update is reported as a
        conflict, never silently overwritten.
        """
        envelope = request.sealed_action
        if not isinstance(envelope, dict):
            raise RuntimeProtocolError("INVALID_SEALED_ACTION", "sealed action is required")
        canonical = envelope.get("canonical_action")
        if not isinstance(canonical, dict):
            raise RuntimeProtocolError("INVALID_SEALED_ACTION", "sealed action payload is invalid")
        if envelope.get("owner_user_id") != request.user_id or envelope.get("branch") != branch:
            raise RuntimeProtocolError("PROPOSAL_BINDING_MISMATCH", "sealed action binding does not match request")
        base_head = envelope.get("base_head_sha")
        if not isinstance(base_head, str) or request.expected_head_sha != base_head:
            raise RuntimeProtocolError("PROPOSAL_BINDING_MISMATCH", "sealed action base revision does not match request")
        payload_digest = envelope.get("payload_digest")
        if not isinstance(payload_digest, str) or payload_digest != "sha256:" + digest_payload(canonical):
            raise RuntimeProtocolError("PROPOSAL_INTEGRITY_FAILED", "sealed action digest does not match")
        from .approvals.contracts import PendingActionService

        integrity = PendingActionService.verify_pending_action(canonical)
        if integrity is not None:
            raise RuntimeProtocolError("PROPOSAL_INTEGRITY_FAILED", "sealed action approval contract is invalid")
        execution_spec = canonical.get("execution_spec")
        if not isinstance(execution_spec, dict) or not isinstance(execution_spec.get("mutation_plan"), dict):
            raise RuntimeProtocolError("INVALID_SEALED_ACTION", "sealed mutation plan is missing")
        binding = execution_spec.get("runtime_binding")
        if not isinstance(binding, dict):
            raise RuntimeProtocolError("PROPOSAL_BINDING_MISMATCH", "sealed action runtime binding is missing")
        if binding.get("owner") != request.user_id:
            raise RuntimeProtocolError("PROPOSAL_BINDING_MISMATCH", "sealed action owner does not match request")
        if binding.get("branch") != branch or binding.get("base_head_sha") != base_head:
            raise RuntimeProtocolError("PROPOSAL_BINDING_MISMATCH", "sealed action target does not match request")
        if binding.get("ledger") != asdict(config):
            raise RuntimeProtocolError("PROPOSAL_BINDING_MISMATCH", "sealed action ledger configuration does not match request")
        try:
            from .mutations.plans import MutationPlan

            plan = MutationPlan.from_spec(execution_spec["mutation_plan"])
        except (TypeError, ValueError) as exc:
            raise RuntimeProtocolError("INVALID_SEALED_ACTION", "sealed mutation plan is invalid") from exc
        with self._lifecycle.open(
            repo_url=request.repo.url,
            token=request.repo.token,
            user_id=request.user_id,
            branch=branch,
            prefix="bean_apply_",
            preflight_mode=PreflightMode.VALIDATE,
            ledger_config=config,
        ) as prepared:
            current_head = _head_sha(prepared.path)
            if current_head != base_head:
                return _runtime_error("CONCURRENT_REVISION", "repository branch changed since approval", retryable=False)
            touched, publication, validation_error = MutationExecutor().apply_and_publish(
                prepared.path,
                plan,
                request.repo.url,
                self._lifecycle._git_service,
                request.repo.token,
                config,
            )
            if validation_error:
                return _runtime_error("LEDGER_INVALID", "approved mutation failed final validation")
            if not publication.get("ok") or str(publication.get("push") or "").startswith("PUSH_FAILED"):
                return _runtime_error("PUBLICATION_CONFLICT", "approved mutation could not be published", retryable=True)
            commit_sha = publication.get("commit_sha")
            if not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
                return _runtime_error("PUBLICATION_UNCONFIRMED", "publication did not return a commit receipt", retryable=True)
            return {
                "status": "ok",
                "receipt": {
                    "operation_id": request.execution_id,
                    "commit_sha": commit_sha,
                    "published_at": date.today().isoformat(),
                    "status": "published",
                    "branch": branch,
                    "changed_paths": list(touched),
                },
            }

    def _report(
        self, arguments: dict[str, Any], workspace: str, config: LedgerConfig
    ) -> dict[str, Any]:
        _argument_keys(arguments, {"report_kind", "date_from", "date_to", "account", "limit"})
        kind = arguments.get("report_kind")
        if kind not in REPORT_KINDS:
            raise RuntimeProtocolError("INVALID_REPORT_KIND", "report_kind is not allowlisted")
        limit = _bounded_limit(arguments, MAX_REPORT_LIMIT)
        date_from = (
            _iso_date(arguments["date_from"], "date_from") if arguments.get("date_from") else None
        )
        date_to = _iso_date(arguments["date_to"], "date_to") if arguments.get("date_to") else None
        account = _optional_text(arguments, "account", 300)
        if kind == "account_activity":
            result = LedgerQueryService.find_transactions(
                workspace,
                account=account,
                date_from=date_from,
                date_to=date_to,
                limit=limit,
                ledger_config=config,
            )
            return {
                "status": "ok" if result.status == "SUCCESS" else "error",
                "report_kind": kind,
                "rows": result.rows,
                "count": result.count,
                "total": result.total,
            }
        account_clause = f' AND account ~ "^{_escape_bql(account)}"' if account else ""
        date_clause = ""
        if date_from:
            date_clause += f" AND date >= {date_from}"
        if date_to:
            date_clause += f" AND date <= {date_to}"
        queries = {
            "account_balance": f"SELECT account, sum(position) AS balance WHERE TRUE{account_clause}{date_clause} GROUP BY account ORDER BY account",
            "trial_balance": f"SELECT account, sum(position) AS balance WHERE TRUE{account_clause}{date_clause} GROUP BY account ORDER BY account",
            "income_statement": f'SELECT account, sum(position) AS total WHERE account ~ "^(Income|Expenses):"{account_clause}{date_clause} GROUP BY account ORDER BY account',
            "cash_flow": f'SELECT account, sum(position) AS total WHERE account ~ "^(Assets|Liabilities):"{account_clause}{date_clause} GROUP BY account ORDER BY account',
        }
        rows, error = Beancount.run_bql_rows(workspace, queries[kind], config)
        if error:
            return _runtime_error("REPORT_FAILED", "allowlisted report failed")
        return {
            "status": "ok",
            "report_kind": kind,
            "rows": rows[:limit],
            "count": min(len(rows), limit),
            "total": len(rows),
            "truncated": len(rows) > limit,
        }

    def _prepare(
        self,
        arguments: dict[str, Any],
        workspace: str,
        config: LedgerConfig,
        *,
        owner: str = "",
        branch: str = "",
        head_sha: str = "",
    ) -> dict[str, Any]:
        _argument_keys(arguments, {"operations", "commit_message", "whitelist"})
        operations = arguments.get("operations")
        if not isinstance(operations, list) or not operations or len(operations) > MAX_OPERATIONS:
            raise RuntimeProtocolError("INVALID_ARGUMENTS", "operations must contain 1 to 25 items")
        commit_message = arguments.get("commit_message", "")
        if not isinstance(commit_message, str) or len(commit_message) > 200:
            raise RuntimeProtocolError("INVALID_ARGUMENTS", "commit_message is too long")
        whitelist = arguments.get("whitelist")
        if whitelist is not None and (
            not isinstance(whitelist, list)
            or len(whitelist) > 50
            or not all(isinstance(item, str) for item in whitelist)
        ):
            raise RuntimeProtocolError("INVALID_ARGUMENTS", "whitelist is invalid")
        prepared_items: list[PreparedMutation] = []
        for operation in operations:
            prepared_items.append(self._build_operation(operation, workspace, config, whitelist))
        plan_operations = tuple(
            item for prepared in prepared_items for item in prepared.plan.operations
        )
        semantic_facts = tuple(
            dict.fromkeys(
                fact for prepared in prepared_items for fact in prepared.plan.semantic_facts
            )
        )
        from .mutations.plans import MutationPlan

        plan = MutationPlan(
            plan_operations,
            commit_message or "chore(ledger): apply change set",
            "Fix the structured changes and prepare them again.",
            semantic_facts=semantic_facts,
        )
        validation = self._preparation._validator.validate(workspace, plan, config)
        if isinstance(validation, PlanValidation) and validation.failure:
            return {"status": "error", "error": asdict(validation.failure)}
        if not isinstance(validation, PlanValidation):
            raise RuntimeProtocolError("PREPARATION_FAILED", "change set validation failed")
        sealed = MutationCoordinator.seal(workspace, plan, config).to_spec()
        review = {
            "title": "Review ledger change set",
            "owner": owner,
            "branch": branch,
            "base_head_sha": head_sha or _head_sha(workspace),
            "operations": [item.display_fields for item in prepared_items],
            "validation": asdict(validation.validation),
            "target_files": sorted(
                {
                    operation.target_file
                    for item in prepared_items
                    for operation in item.plan.operations
                    if operation.target_file
                }
                | {sidecar_target_file(config)}
            ),
        }
        action_payload = asdict(
            PendingActionService.create_pending_action(
                action_type="change_set",
                execution_spec={
                    "mutation_plan": sealed,
                    "runtime_binding": {
                        "owner": owner,
                        "branch": branch,
                        "ledger": asdict(config),
                        "base_head_sha": head_sha or _head_sha(workspace),
                    },
                },
                display={"kind": "change_set_preview", "review": review},
                validation=asdict(validation.validation),
            )
        )
        return {
            "status": "approval_required",
            "action": action_payload,
            "review": review,
            "plan_digest": _canonical_digest(sealed),
            "base_head_sha": _head_sha(workspace),
        }

    def _build_operation(
        self, operation: object, workspace: str, config: LedgerConfig, whitelist: list[str] | None
    ) -> PreparedMutation:
        if not isinstance(operation, dict) or set(operation) - {
            "kind",
            "transaction",
            "transaction_ref",
            "revision_fingerprint",
            "account_name",
            "currency",
            "open_date",
            "display_name",
            "close_date",
            "price_date",
            "base_commodity",
            "price",
            "quote_commodity",
            "source",
            "effective_at",
            "observed_date",
            "amount",
            "assertion_date",
            "adjustment_account",
            "cutoff",
            "reason",
        }:
            raise RuntimeProtocolError("INVALID_ARGUMENTS", "operation has unsupported fields")
        kind = operation.get("kind")
        if kind not in STRUCTURED_OPERATION_KEYS:
            raise RuntimeProtocolError("UNSUPPORTED_CHANGE", "operation kind is not supported")
        kwargs: dict[str, Any]
        handler_key: str
        if kind == "create_transaction":
            handler_key, kwargs = (
                "commit_transaction",
                {
                    "transaction_text": _render_transaction(operation.get("transaction")),
                    "commit_message": "",
                    "whitelist": whitelist,
                },
            )
        elif kind == "update_transaction":
            handler_key, kwargs = (
                "update_transaction",
                {
                    "transaction_ref": _require_operation_text(operation, "transaction_ref"),
                    "revision_fingerprint": _require_operation_text(
                        operation, "revision_fingerprint"
                    ),
                    "new_transaction_text": _render_transaction(operation.get("transaction")),
                    "commit_message": "",
                    "whitelist": whitelist,
                },
            )
        elif kind == "delete_transaction":
            reason = _require_operation_text(operation, "reason")
            handler_key, kwargs = (
                "delete_transaction",
                {
                    "transaction_ref": _require_operation_text(operation, "transaction_ref"),
                    "revision_fingerprint": _require_operation_text(
                        operation, "revision_fingerprint"
                    ),
                    "commit_message": "",
                },
            )
        elif kind == "open_account":
            handler_key, kwargs = (
                "open_account",
                {
                    "account_name": _require_operation_text(operation, "account_name"),
                    "currency": _currency(operation.get("currency")),
                    "open_date": _iso_date(operation.get("open_date"), "open_date"),
                    "display_name": _optional_operation_text(operation, "display_name", 200),
                },
            )
        elif kind == "close_account":
            handler_key, kwargs = (
                "close_account",
                {
                    "account_name": _require_operation_text(operation, "account_name"),
                    "close_date": _iso_date(operation.get("close_date"), "close_date"),
                    "commit_message": "",
                },
            )
        elif kind == "set_price":
            handler_key, kwargs = (
                "price",
                {
                    key: _require_operation_text(operation, key)
                    for key in (
                        "price_date",
                        "base_commodity",
                        "price",
                        "quote_commodity",
                        "source",
                        "effective_at",
                    )
                },
            )
            kwargs["commit_message"] = ""
        else:
            if operation.get("assertion_date") is not None:
                handler_key, kwargs = (
                    "balance_update",
                    {
                        key: _require_operation_text(operation, key)
                        for key in (
                            "assertion_date",
                            "account",
                            "currency",
                            "adjustment_account",
                        )
                    },
                )
                kwargs["commit_message"] = ""
            else:
                handler_key, kwargs = (
                    "balance_reconciliation",
                    {
                        key: _require_operation_text(operation, key)
                        for key in (
                            "observed_date",
                            "account",
                            "amount",
                            "currency",
                            "adjustment_account",
                        )
                    },
                )
                kwargs.update(
                    {"cutoff": operation.get("cutoff", "end_of_day"), "commit_message": ""}
                )
        prepared = self._registry.get(handler_key).build(workspace, config, **kwargs)
        if isinstance(prepared, (InvariantViolation, ValidationFailed)):
            raise RuntimeProtocolError(
                prepared.status, "structured operation failed deterministic policy"
            )
        if kind == "delete_transaction":
            from dataclasses import replace

            prepared = replace(
                prepared,
                display_fields={**prepared.display_fields, "review_reason": reason},
                validation_fields={**prepared.validation_fields, "review_reason": reason},
            )
        return prepared


def _require_operation_text(operation: dict[str, Any], key: str) -> str:
    value = operation.get(key)
    try:
        return _single_line(value, f"operation.{key}", 1_000)
    except RuntimeProtocolError as exc:
        raise RuntimeProtocolError("INVALID_ARGUMENTS", str(exc)) from exc


def _optional_operation_text(operation: dict[str, Any], key: str, max_length: int) -> str | None:
    value = operation.get(key)
    if value is None:
        return None
    try:
        return _single_line(value, f"operation.{key}", max_length)
    except RuntimeProtocolError as exc:
        raise RuntimeProtocolError("INVALID_ARGUMENTS", str(exc)) from exc


def _currency(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9\-]{0,14}", value):
        raise RuntimeProtocolError("INVALID_ARGUMENTS", "operation.currency is invalid")
    return value


def _escape_bql(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _head_sha(workspace: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True, check=False
    )
    if result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", result.stdout.strip()):
        raise RuntimeProtocolError("REPOSITORY_HEAD_UNAVAILABLE", "repository head is unavailable")
    return result.stdout.strip()


def _runtime_error(
    code: str, message: str, *, retryable: bool = False, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "status": "error",
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            **({"details": details} if details else {}),
        },
    }
