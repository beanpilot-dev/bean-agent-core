"""Compatibility façade for focused agent-core operation handlers."""

import tempfile as _stdlib_tempfile
from typing import AsyncGenerator

from .onboarding import SetupOperation
from .operations.cache import CacheWarmupOperationHandler, safe_git_message
from .operations.chat import ChatOperationHandler
from .operations.ledger_reads import LedgerReadOperationHandler
from .operations.lifecycle import RequestWorkspaceLifecycle, git_error_code
from .operations.onboarding import OnboardingOperationHandler
from .operations.pending_actions import PendingActionOperationHandler
from .preflight import PreflightService
from .types import LedgerConfig
from .workspace import CachedWorkspaceManager, GitService, GitServiceError

__all__ = ["AgentOrchestrator", "OrchestratorError", "PreflightService"]


class _TempfileCompatibility:
    """Keep the historical test seam without monkeypatching stdlib globally."""

    @staticmethod
    def mkdtemp(**kwargs) -> str:
        return _stdlib_tempfile.mkdtemp(**kwargs)


tempfile = _TempfileCompatibility()


class OrchestratorError(Exception):
    """Unrecoverable orchestration error."""


def _git_error_code(error: GitServiceError) -> str:
    return git_error_code(error)


def _safe_git_message(code: str) -> str:
    return safe_git_message(code)


class AgentOrchestrator:
    """Small compatibility façade over operation-specific handlers."""

    def __init__(
        self,
        agent,
        cache_manager: CachedWorkspaceManager,
        git_service: GitService,
    ) -> None:
        lifecycle = RequestWorkspaceLifecycle(
            cache_manager,
            git_service,
            workspace_factory=lambda prefix: tempfile.mkdtemp(prefix=prefix),
        )
        self._chat = ChatOperationHandler(agent, lifecycle, git_service)
        self._ledger_reads = LedgerReadOperationHandler(lifecycle)
        self._pending_actions = PendingActionOperationHandler(lifecycle, git_service)
        self._cache_warmup = CacheWarmupOperationHandler(lifecycle)
        self._onboarding = OnboardingOperationHandler(lifecycle, git_service)

    async def run(
        self,
        *,
        workspace_path: str,
        repo_url: str,
        token: str | None,
        agent_run_id: str | None,
        user_id: str,
        request_id: str | None,
        api_key: str,
        model: str,
        query: str,
        conversation_meta: dict | None,
        messages: list[dict],
        ledger_config: LedgerConfig | None = None,
    ) -> AsyncGenerator[dict, None]:
        async for chunk in self._chat.run(
            workspace_path=workspace_path,
            repo_url=repo_url,
            token=token,
            agent_run_id=agent_run_id,
            user_id=user_id,
            request_id=request_id,
            api_key=api_key,
            model=model,
            query=query,
            conversation_meta=conversation_meta,
            messages=messages,
            ledger_config=ledger_config,
        ):
            yield chunk

    async def run_stats(
        self,
        *,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
        tag: str,
        ledger_config: LedgerConfig | None = None,
    ) -> dict:
        return await self._ledger_reads.run_stats(
            repo_url=repo_url,
            token=token,
            user_id=user_id,
            request_id=request_id,
            tag=tag,
            ledger_config=ledger_config,
        )

    async def run_apply_pending_action(
        self,
        *,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
        pending_action: dict,
        ledger_config: LedgerConfig | None = None,
    ) -> dict:
        return await self._pending_actions.run(
            repo_url=repo_url,
            token=token,
            user_id=user_id,
            request_id=request_id,
            pending_action=pending_action,
            ledger_config=ledger_config,
        )

    async def run_accounts(
        self,
        *,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
        ledger_config: LedgerConfig | None = None,
    ) -> dict:
        return await self._ledger_reads.run_accounts(
            repo_url=repo_url,
            token=token,
            user_id=user_id,
            request_id=request_id,
            ledger_config=ledger_config,
        )

    async def run_cache_warmup(
        self,
        *,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
        ledger_config: LedgerConfig | None = None,
    ) -> dict:
        return await self._cache_warmup.run(
            repo_url=repo_url,
            token=token,
            user_id=user_id,
            request_id=request_id,
            ledger_config=ledger_config,
        )

    async def run_onboarding_discovery(
        self,
        *,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
        entry_path: str | None,
        expected_head_sha: str | None,
    ) -> dict:
        return await self._onboarding.run_discovery(
            repo_url=repo_url,
            token=token,
            user_id=user_id,
            request_id=request_id,
            entry_path=entry_path,
            expected_head_sha=expected_head_sha,
        )

    async def run_onboarding_setup_preview(
        self,
        *,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
        operation: SetupOperation,
        entry_path: str | None,
        sidecar_main_path: str | None,
        sidecar_write_dir: str | None,
        ledger_title: str | None = None,
        operating_currency: str | None = None,
    ) -> dict:
        return await self._onboarding.run_setup_preview(
            repo_url=repo_url,
            token=token,
            user_id=user_id,
            request_id=request_id,
            operation=operation,
            entry_path=entry_path,
            sidecar_main_path=sidecar_main_path,
            sidecar_write_dir=sidecar_write_dir,
            ledger_title=ledger_title,
            operating_currency=operating_currency,
        )

    async def run_onboarding_setup_confirm(
        self,
        *,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
        operation: SetupOperation,
        expected_head_sha: str,
        entry_path: str | None,
        sidecar_main_path: str | None,
        sidecar_write_dir: str | None,
        ledger_title: str | None = None,
        operating_currency: str | None = None,
    ) -> dict:
        return await self._onboarding.run_setup_confirm(
            repo_url=repo_url,
            token=token,
            user_id=user_id,
            request_id=request_id,
            operation=operation,
            expected_head_sha=expected_head_sha,
            entry_path=entry_path,
            sidecar_main_path=sidecar_main_path,
            sidecar_write_dir=sidecar_write_dir,
            ledger_title=ledger_title,
            operating_currency=operating_currency,
        )
