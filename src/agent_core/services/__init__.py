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
from .preflight import PreflightService
from .prices import PriceService
from .types import (
    DEFAULT_LEDGER_CONFIG,
    CommitResult,
    DependencyUnavailable,
    FileReadResult,
    InvariantViolation,
    LedgerConfig,
    PreflightResult,
    Preview,
    PriceResult,
    ProposalStore,
    QueryResult,
    SandboxResult,
    ServiceResult,
    ValidationFailed,
)
from .workspace import GitService

__all__ = [
    # Services
    "AgentOrchestrator",
    "GitService",
    "IngestionService",
    "LedgerService",
    "PreflightService",
    "PriceService",
    # Types
    "CommitResult",
    "DependencyUnavailable",
    "DEFAULT_LEDGER_CONFIG",
    "FileReadResult",
    "InvariantViolation",
    "LedgerConfig",
    "PreflightResult",
    "Preview",
    "PriceResult",
    "ProposalStore",
    "QueryResult",
    "SandboxResult",
    "ServiceResult",
    "ValidationFailed",
]
