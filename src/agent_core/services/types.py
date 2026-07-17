"""Typed return values for the Service Layer.

Every service method returns a concrete ServiceResult subclass so the
caller (Tool Layer or API Layer) can pattern-match on the type rather
than inspecting string dict keys.
"""

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal

# ── Base ──────────────────────────────────────────────────────────────────────

@dataclass
class ServiceResult:
    """Base result for all service operations. Subclassed for each outcome."""
    status: str


# ── Proposal ──────────────────────────────────────────────────────────────────

@dataclass
class Preview(ServiceResult):
    """A proposal awaiting user confirmation.

    The LLM sees the preview details and asks the user to approve.
    proposal_id is passed to the corresponding confirm_* method.
    """
    status: Literal["PREVIEW"] = "PREVIEW"
    proposal_id: str = ""
    operation: str = ""
    preview: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    pending_action: dict[str, Any] | None = None


@dataclass
class LedgerMutationAction:
    """Runtime-neutral ledger mutation intent validated before approval."""

    action_type: str = ""
    schema_version: int = 1
    execution_spec: dict[str, Any] = field(default_factory=dict)
    display: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)


@dataclass
class ApprovalRequired(ServiceResult):
    """Runtime-neutral outcome for a validated mutation awaiting approval.

    SaaS may persist the payload opaquely and use the digest/signature metadata
    for idempotency and tamper detection. The display fields are informational;
    execution always uses execution_spec.
    """

    status: Literal["PENDING_ACTION"] = "PENDING_ACTION"
    pending_action_id: str = ""
    action_type: str = ""
    schema_version: int = 1
    execution_spec: dict[str, Any] = field(default_factory=dict)
    display: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    expires_at: str = ""
    idempotency_key: str = ""
    continue_after_approval: bool = False
    continuation_reason: str = ""
    next_intent_summary: str = ""
    digest: str = ""
    signature: str = ""
    message: str = ""


@dataclass
class PendingAction(ApprovalRequired):
    """Backward-compatible name for approval-gated ledger mutations."""


@dataclass
class ApplyReceipt(ServiceResult):
    """Deterministic apply result for a previously approved pending action."""

    status: Literal["APPLIED"] = "APPLIED"
    pending_action_id: str = ""
    action_type: str = ""
    receipt: dict[str, Any] = field(default_factory=dict)


@dataclass
class IntegrityFailed(ServiceResult):
    """Pending action payload failed deterministic integrity checks."""

    status: Literal["INTEGRITY_FAILED"] = "INTEGRITY_FAILED"
    pending_action_id: str = ""
    error: str = ""


# ── Runtime-neutral tool outcomes ────────────────────────────────────────────

ToolExecutionStatus = Literal["completed", "repairable_error", "approval_required"]


@dataclass
class ToolCompleted(ServiceResult):
    """Runtime-neutral outcome for a tool call that completed."""

    status: Literal["completed"] = "completed"
    tool_name: str = ""
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolRepairableError(ServiceResult):
    """Runtime-neutral outcome for a tool error the caller may revise and retry."""

    status: Literal["repairable_error"] = "repairable_error"
    tool_name: str = ""
    error_type: str = ""
    message: str = ""
    remediation: str = ""
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolApprovalRequired(ServiceResult):
    """Runtime-neutral outcome for a validated mutation awaiting host approval.

    pending_action is the immutable executable contract. Hosts may store it
    opaquely and later submit the same payload with approval proof to a trusted
    apply surface.
    """

    status: Literal["approval_required"] = "approval_required"
    tool_name: str = ""
    action_type: str = ""
    pending_action: dict[str, Any] = field(default_factory=dict)
    display: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    message: str = ""


@dataclass(frozen=True)
class ApprovalProof:
    """Host-controlled proof that a human approved the immutable action payload."""

    approved_by: str
    approved_at: str
    approval_id: str
    pending_action_id: str = ""
    payload_digest: str = ""
    integrity_digest: str = ""
    host: str = ""


# ── Success ───────────────────────────────────────────────────────────────────

@dataclass
class CommitResult(ServiceResult):
    """Operation completed and pushed to Git."""
    status: Literal["SUCCESS"] = "SUCCESS"
    outcome: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    push_status: str | None = None
    commit_sha: str | None = None


# ── Domain errors (LLM can self-correct) ──────────────────────────────────────

@dataclass
class InvariantViolation(ServiceResult):
    """Business rule violation — returned to LLM for self-correction."""
    status: Literal["INVARIANT_VIOLATION"] = "INVARIANT_VIOLATION"
    invariant: str = ""
    severity: str = ""
    provided: Any = None
    remediation: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationFailed(ServiceResult):
    """bean-check failed — write was auto-reverted."""
    status: Literal["VALIDATION_FAILED"] = "VALIDATION_FAILED"
    error: str = ""
    reverted: bool = True
    remediation: str = ""
    advisory: dict[str, Any] | None = None


@dataclass
class ValidationSummary:
    """Sanitized deterministic validation details for previews and repairs."""

    status: Literal["validated", "failed"] = "validated"
    validator: str = "bean-check"
    isolated: bool = True
    error_type: str | None = None
    error_count: int = 0
    messages: list[str] = field(default_factory=list)
    retryable: bool = False


@dataclass
class DependencyUnavailable(ServiceResult):
    """Infrastructure failure — git, filesystem, network."""
    status: Literal["DEPENDENCY_UNAVAILABLE"] = "DEPENDENCY_UNAVAILABLE"
    error: str = ""
    retryable: bool = False


# ── Read results ──────────────────────────────────────────────────────────────

@dataclass
class QueryResult(ServiceResult):
    """BQL query or transaction search result."""
    status: Literal["SUCCESS", "ERROR"] = "SUCCESS"
    count: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    truncated: bool = False
    omitted: int = 0
    filters_applied: dict[str, Any] | None = None
    account: str | None = None
    as_of: str | None = None
    balance: str | None = None
    error: str | None = None
    error_code: str | None = None
    bql: str | None = None
    transaction: dict[str, Any] | None = None
    transaction_ref: str | None = None
    directive: str | None = None
    source_path: str | None = None
    source_start_line: int | None = None
    source_end_line: int | None = None
    payee: str | None = None
    narration: str | None = None
    tags: list[str] | None = None
    links: list[str] | None = None
    metadata: dict[str, Any] | None = None
    postings: list[dict[str, Any]] | None = None
    revision_fingerprint: str | None = None


@dataclass
class AccountSearchResult(ServiceResult):
    """Bounded deterministic account lookup results."""

    status: Literal["SUCCESS", "ERROR"] = "SUCCESS"
    query: str = ""
    account_type: str = ""
    lifecycle_status: str = "open"
    limit: int = 20
    candidates: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0
    total: int = 0
    truncated: bool = False
    omitted: int = 0
    error: str | None = None


@dataclass
class PreflightResult(ServiceResult):
    """Ledger preflight check result."""
    status: Literal["CLEAN", "ERROR", "SETUP_REQUIRED"] = "CLEAN"
    target: str | None = None
    accounts: list[str] = field(default_factory=list)
    accounts_by_type: dict[str, list[str]] = field(default_factory=dict)
    accounts_truncated: bool = False
    accounts_omitted: int = 0
    errors: str | None = None
    recent: str | None = None
    action: str | None = None
    ledger_meta: dict[str, Any] | None = None
    balance_snapshot: dict[str, Any] | None = None
    flow_summary: dict[str, Any] | None = None
    recent_activity: dict[str, Any] | None = None
    recent_ledger_text: dict[str, Any] | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)


# ── Ledger config ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LedgerConfig:
    """Per-request ledger layout supplied by srv.

    Defaults preserve direct/local agent-core usability when the caller omits the
    ledger object. The config is never persisted by agent-core.
    """

    entry_path: str = "data/main.beancount"
    sidecar_main_path: str | None = "data/agent_inc/main.beancount"
    sidecar_write_dir: str = "data/agent_inc"

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_path", _normalize_repo_path(self.entry_path))
        object.__setattr__(
            self,
            "sidecar_write_dir",
            _normalize_repo_path(self.sidecar_write_dir),
        )

        derived_sidecar_main = (
            PurePosixPath(self.sidecar_write_dir) / "main.beancount"
        ).as_posix()
        provided_sidecar_main = (
            _normalize_repo_path(self.sidecar_main_path)
            if self.sidecar_main_path
            else derived_sidecar_main
        )
        sidecar_parent = PurePosixPath(provided_sidecar_main).parent.as_posix()
        sidecar_name = PurePosixPath(provided_sidecar_main).name
        if sidecar_parent != self.sidecar_write_dir or sidecar_name != "main.beancount":
            provided_sidecar_main = derived_sidecar_main
        object.__setattr__(self, "sidecar_main_path", provided_sidecar_main)


def _normalize_repo_path(path: str) -> str:
    normalized = PurePosixPath(path).as_posix().strip("/")
    parts = PurePosixPath(normalized).parts
    if not normalized or path.startswith("/") or ".." in parts:
        raise ValueError("ledger paths must be relative repository paths")
    return normalized


DEFAULT_LEDGER_CONFIG = LedgerConfig()


# ── External data ─────────────────────────────────────────────────────────────

@dataclass
class PriceResult(ServiceResult):
    """Typed result for an external market quote lookup."""

    status: Literal["SUCCESS", "ERROR"] = "SUCCESS"
    instrument: str = ""
    price: float | None = None
    quote_currency: str = ""
    provider: str = ""
    effective_date: str | None = None
    effective_at: str | None = None
    freshness: Literal["daily", "intraday", "previous_close"] | None = None
    market_state: str | None = None
    exchange: str | None = None
    error_code: str | None = None
    error_message: str | None = None


# ── Ingestion ─────────────────────────────────────────────────────────────────

@dataclass
class FileReadResult(ServiceResult):
    """File read result."""
    status: Literal["SUCCESS", "ERROR"] = "SUCCESS"
    file_path: str = ""
    size_bytes: int = 0
    lines: int = 0
    content: str = ""
    error: str | None = None


@dataclass
class SandboxResult(ServiceResult):
    """Python sandbox execution result."""
    status: Literal["SUCCESS", "ERROR"] = "SUCCESS"
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    staging_file: str | None = None
    transaction_count: int = 0
    sample: str = ""
    error: str | None = None
