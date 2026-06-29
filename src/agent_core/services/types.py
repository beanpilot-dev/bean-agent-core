"""Typed return values for the Service Layer.

Every service method returns a concrete ServiceResult subclass so the
caller (Tool Layer or API Layer) can pattern-match on the type rather
than inspecting string dict keys.
"""

import uuid
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
class PendingAction(ServiceResult):
    """Immutable approval-gated action prepared by agent-core.

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
    digest: str = ""
    signature: str = ""
    message: str = ""


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


# ── Success ───────────────────────────────────────────────────────────────────

@dataclass
class CommitResult(ServiceResult):
    """Operation completed and pushed to Git."""
    status: Literal["SUCCESS"] = "SUCCESS"
    outcome: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    push_status: str | None = None


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
    filters_applied: dict[str, Any] | None = None
    template: str | None = None
    params: dict[str, Any] | None = None
    account: str | None = None
    as_of: str | None = None
    balance: str | None = None
    error: str | None = None
    bql: str | None = None


@dataclass
class PreflightResult(ServiceResult):
    """Ledger preflight check result."""
    status: Literal["CLEAN", "ERROR", "SETUP_REQUIRED"] = "CLEAN"
    target: str | None = None
    accounts: list[str] = field(default_factory=list)
    errors: str | None = None
    recent: str | None = None
    action: str | None = None


# ── Ledger config ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class LedgerConfig:
    """Per-request ledger layout supplied by srv.

    Defaults preserve direct/local agent-core usability when the caller omits the
    ledger object. The config is never persisted by agent-core.
    """

    entry_path: str = "data/main.beancount"
    sidecar_main_path: str = "data/agent_inc/main.beancount"
    sidecar_write_dir: str = "data/agent_inc"

    def __post_init__(self) -> None:
        object.__setattr__(self, "entry_path", _normalize_repo_path(self.entry_path))
        object.__setattr__(
            self,
            "sidecar_main_path",
            _normalize_repo_path(self.sidecar_main_path),
        )
        object.__setattr__(
            self,
            "sidecar_write_dir",
            _normalize_repo_path(self.sidecar_write_dir),
        )

        sidecar_parent = PurePosixPath(self.sidecar_main_path).parent.as_posix()
        if sidecar_parent != self.sidecar_write_dir:
            raise ValueError("sidecar_main_path must live inside sidecar_write_dir")


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
    """Market price fetch result."""
    status: Literal["SUCCESS", "ERROR"] = "SUCCESS"
    symbol: str = ""
    price: float = 0.0
    currency: str = ""
    source: str = ""
    date: str | None = None
    exchange: str | None = None
    error: str | None = None


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


# ── Proposal store ────────────────────────────────────────────────────────────

class ProposalStore:
    """Per-request in-memory store for in-flight preview proposals.

    Created by the orchestrator at request start and shared with LedgerService
    so preview_* methods can stash proposal data and confirm_* methods can
    retrieve it by proposal_id.
    """

    def __init__(self):
        self._store: dict[str, dict[str, Any]] = {}

    def create(self, operation: str, data: dict[str, Any]) -> str:
        """Store proposal data and return a unique proposal_id."""
        pid = f"prop_{uuid.uuid4().hex[:12]}"
        self._store[pid] = {"operation": operation, "data": data}
        return pid

    def get(self, proposal_id: str) -> dict[str, Any] | None:
        """Retrieve proposal data by id, or None if not found."""
        return self._store.get(proposal_id)

    def remove(self, proposal_id: str) -> None:
        """Remove a consumed proposal."""
        self._store.pop(proposal_id, None)
