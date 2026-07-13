"""Service Layer — deterministic infrastructure and business logic.

The service layer contains all operations that do not require an LLM:
  - Git clone/pull/push (GitService)
  - Beancount read/write business logic (LedgerService)
  - Preflight validation (PreflightService)
  - File ingestion and sandboxed Python (IngestionService)
  - External price fetching (PriceService)
  - LLM orchestration and streaming (AgentOrchestrator)
  - Typed return values (Preview, CommitResult, InvariantViolation, etc.)

Services accept explicit parameters (workspace path, token, etc.) — they do
NOT depend on ContextVars. Write operations use a preview→confirm split
where confirm_* re-runs preview validation internally.

The tool layer (agent.py) wraps services for LLM consumption.
"""

from .ingestion import IngestionService
from .ledger import LedgerService
from .orchestrator import AgentOrchestrator
from .pending_actions import PendingActionService, digest_payload
from .preflight import PreflightService
from .prices import PriceService
from .tool_gateway import ToolExecutionGateway
from .types import (
    DEFAULT_LEDGER_CONFIG,
    ApplyReceipt,
    ApprovalProof,
    ApprovalRequired,
    CommitResult,
    DependencyUnavailable,
    FileReadResult,
    IntegrityFailed,
    InvariantViolation,
    LedgerConfig,
    PendingAction,
    PreflightResult,
    Preview,
    PriceResult,
    QueryResult,
    SandboxResult,
    ServiceResult,
    ToolApprovalRequired,
    ToolCompleted,
    ToolRepairableError,
    ValidationFailed,
)
from .workspace import GitService

__all__ = [
    # Services
    "AgentOrchestrator",
    "GitService",
    "IngestionService",
    "LedgerService",
    "PendingActionService",
    "PreflightService",
    "PriceService",
    "ToolExecutionGateway",
    # Types
    "ApprovalProof",
    "ApprovalRequired",
    "ApplyReceipt",
    "CommitResult",
    "DependencyUnavailable",
    "DEFAULT_LEDGER_CONFIG",
    "FileReadResult",
    "IntegrityFailed",
    "InvariantViolation",
    "LedgerConfig",
    "PendingAction",
    "PreflightResult",
    "Preview",
    "PriceResult",
    "QueryResult",
    "SandboxResult",
    "ServiceResult",
    "ToolApprovalRequired",
    "ToolCompleted",
    "ToolRepairableError",
    "ValidationFailed",
    "digest_payload",
]
