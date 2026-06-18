"""AgentOrchestrator — wires Service + Agent layers together.

Handles the full lifecycle:
  1. Workspace setup (create temp dir, git clone/pull from cache)
  2. Preflight validation (sidecar check, bean-check, account listing)
  3. LangGraph agent invocation (LLM + tool calling loop)
  4. SSE chunk streaming
  5. Workspace cleanup

The orchestrator is the single entry point from the API layer. It takes
the full request payload, runs deterministic setup, then delegates to
the Agent layer for LLM reasoning.
"""

import logging
import tempfile
import time
from typing import AsyncGenerator

from .ledger import Beancount
from .onboarding import OnboardingService, SetupOperation
from .preflight import PreflightService, SetupRequiredError
from .types import LedgerConfig
from .workspace import (
    CachedWorkspaceManager,
    CacheLockTimeoutError,
    GitService,
    GitServiceError,
    RepoAuthFailedError,
)

logger = logging.getLogger(__name__)


class OrchestratorError(Exception):
    """Unrecoverable orchestration error."""


def _git_error_code(error: GitServiceError) -> str:
    return "REPO_AUTH_FAILED" if isinstance(error, RepoAuthFailedError) else "REPO_UNREACHABLE"


class AgentOrchestrator:
    """Full lifecycle orchestrator for agent requests.

    Creates a per-request LedgerService that handles write operations
    with preview→confirm split (confirm always re-validates internally).

    Usage from API layer:
        orchestrator = AgentOrchestrator(agent)
        async for chunk in orchestrator.run(request):
            yield format_sse(chunk)
    """

    def __init__(
        self,
        agent,
        cache_manager: CachedWorkspaceManager,
        git_service: GitService,
    ):
        """agent: PersonalFinanceAgent instance (from agent.py)."""
        self._agent = agent
        self._cache_manager = cache_manager
        self._git_service = git_service

    async def run(
        self,
        *,
        workspace_path: str,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
        api_key: str,
        model: str,
        query: str,
        conversation_meta: dict | None,
        messages: list[dict],
        ledger_config: LedgerConfig | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Run the full agent lifecycle and yield SSE chunks."""
        start_time = time.monotonic()

        try:
            self._git_service.validate_request_credentials(repo_url, token)
            logger.info(
                "orchestrator setup user_id=%s request_id=%s",
                user_id, request_id,
            )

            cache_path = self._cache_manager.acquire(user_id, repo_url, token)
            self._git_service.copy(cache_path, workspace_path)

            try:
                PreflightService.validate(workspace_path, ledger_config)
            except SetupRequiredError as e:
                logger.error("Preflight validation failed: SETUP_REQUIRED — %s", e)
                yield {"type": "fatal", "code": "SETUP_REQUIRED", "message": str(e)}
                return

            if conversation_meta is None:
                conversation_meta = {}

            whitelist = conversation_meta.get("account_whitelist")
            async for chunk in self._agent.stream(
                query=query,
                prior=messages,
                conversation_meta=conversation_meta,
                api_key=api_key,
                model=model,
                workspace=workspace_path,
                repo_url=repo_url,
                token=token,
                git_service=self._git_service,
                whitelist=whitelist,
                ledger_config=ledger_config,
            ):
                yield chunk

        except CacheLockTimeoutError as e:
            logger.error("Cache lock timeout in run()")
            yield {"type": "fatal", "code": "INTERNAL_ERROR", "message": str(e)}

        except GitServiceError as e:
            logger.error(
                "Git error during orchestration code=%s error_type=%s",
                _git_error_code(e),
                type(e).__name__,
            )
            yield {"type": "fatal", "code": _git_error_code(e), "message": str(e)}

        except Exception as e:
            logger.error("Orchestrator error error_type=%s", type(e).__name__)
            duration_ms = int((time.monotonic() - start_time) * 1000)
            yield {"type": "fatal", "code": "INTERNAL_ERROR", "message": str(e)}
            yield {
                "type": "history_snapshot",
                "messages": messages,
                "trace_id": None,
                "trace_url": None,
                "usage": {"tokens": 0, "duration_ms": duration_ms},
            }

        finally:
            self._git_service.destroy(workspace_path)

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
        """Run a lightweight stats query (no LLM)."""
        start_time = time.monotonic()
        workspace_path: str | None = None

        try:
            self._git_service.validate_request_credentials(repo_url, token)
            cache_path = self._cache_manager.acquire(user_id, repo_url, token)
            workspace_path = tempfile.mkdtemp(prefix="bean_stats_")
            self._git_service.copy(cache_path, workspace_path)

            tag_clean = tag.lstrip("#")
            bql = (
                f'SELECT account, sum(position) AS total '
                f'WHERE tags("{tag_clean}") GROUP BY account ORDER BY total DESC'
            )
            rows, error = Beancount.run_bql_rows(workspace_path, bql, ledger_config)
            if error:
                bql = (
                    f'SELECT account, sum(position) AS total '
                    f'WHERE narration ~ "{tag}" GROUP BY account ORDER BY total DESC'
                )
                rows, error = Beancount.run_bql_rows(
                    workspace_path, bql, ledger_config,
                )

            if error:
                return {
                    "status": "error",
                    "error": {"code": "INTERNAL_ERROR", "message": error},
                    "tag": tag,
                    "usage": {
                        "duration_ms": int((time.monotonic() - start_time) * 1000),
                    },
                }

            return {
                "status": "ok",
                "tag": tag,
                "rows": rows,
                "usage": {"duration_ms": int((time.monotonic() - start_time) * 1000)},
            }

        except CacheLockTimeoutError as e:
            logger.error("Cache lock timeout in run_stats()")
            return {
                "status": "error",
                "error": {"code": "INTERNAL_ERROR", "message": str(e)},
            }

        except GitServiceError as e:
            return {
                "status": "error",
                "error": {"code": _git_error_code(e), "message": str(e)},
            }
        finally:
            if workspace_path:
                self._git_service.destroy(workspace_path)

    async def run_accounts(
        self,
        *,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
        ledger_config: LedgerConfig | None = None,
    ) -> dict:
        """Run account listing (no LLM)."""
        start_time = time.monotonic()
        workspace_path: str | None = None

        try:
            self._git_service.validate_request_credentials(repo_url, token)
            cache_path = self._cache_manager.acquire(user_id, repo_url, token)
            workspace_path = tempfile.mkdtemp(prefix="bean_accounts_")
            self._git_service.copy(cache_path, workspace_path)

            if not PreflightService.check_setup(workspace_path, ledger_config):
                return {
                    "status": "error",
                    "error": {
                        "code": "SETUP_REQUIRED",
                        "message": (
                            "Sidecar include directive is missing."
                        ),
                    },
                }

            accounts = PreflightService.list_accounts(workspace_path, ledger_config)
            raw = PreflightService.get_raw_open_directives(workspace_path, ledger_config)
            return {
                "status": "ok",
                "accounts": accounts,
                "raw_accounts": raw,
                "usage": {
                    "duration_ms": int((time.monotonic() - start_time) * 1000),
                },
            }

        except CacheLockTimeoutError as e:
            logger.error("Cache lock timeout in run_accounts()")
            return {
                "status": "error",
                "error": {"code": "INTERNAL_ERROR", "message": str(e)},
            }

        except GitServiceError as e:
            return {
                "status": "error",
                "error": {"code": _git_error_code(e), "message": str(e)},
            }
        finally:
            if workspace_path:
                self._git_service.destroy(workspace_path)

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
        """Run deterministic onboarding discovery (no LLM, no mutation)."""
        workspace_path: str | None = None
        try:
            self._git_service.validate_request_credentials(repo_url, token)
            cache_path = self._cache_manager.acquire(user_id, repo_url, token)
            workspace_path = tempfile.mkdtemp(prefix="bean_onboarding_discover_")
            self._git_service.copy(cache_path, workspace_path)
            return OnboardingService.discover(
                workspace_path,
                entry_path=entry_path,
                expected_head_sha=expected_head_sha,
            )
        except CacheLockTimeoutError:
            logger.error("Cache lock timeout in run_onboarding_discovery()")
            return {
                "status": "error",
                "discovery_status": "invalid_request",
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Onboarding discovery is unavailable",
                },
            }
        except GitServiceError as e:
            code = _git_error_code(e)
            return {
                "status": "error",
                "discovery_status": (
                    "repo_auth_failed" if code == "REPO_AUTH_FAILED" else "repo_unreachable"
                ),
                "error": {"code": code, "message": _safe_git_message(code)},
            }
        finally:
            if workspace_path:
                self._git_service.destroy(workspace_path)

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
    ) -> dict:
        """Build deterministic onboarding setup preview without mutation."""
        workspace_path: str | None = None
        try:
            self._git_service.validate_request_credentials(repo_url, token)
            cache_path = self._cache_manager.acquire(user_id, repo_url, token)
            workspace_path = tempfile.mkdtemp(prefix="bean_onboarding_preview_")
            self._git_service.copy(cache_path, workspace_path)
            return OnboardingService.preview_setup(
                workspace_path,
                operation=operation,
                entry_path=entry_path,
                sidecar_main_path=sidecar_main_path,
                sidecar_write_dir=sidecar_write_dir,
            )
        except GitServiceError as e:
            code = _git_error_code(e)
            return {
                "status": "error",
                "code": code,
                "message": _safe_git_message(code),
            }
        finally:
            if workspace_path:
                self._git_service.destroy(workspace_path)

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
    ) -> dict:
        """Apply deterministic onboarding setup after explicit confirmation."""
        workspace_path: str | None = None
        try:
            self._git_service.validate_request_credentials(repo_url, token)
            cache_path = self._cache_manager.acquire(user_id, repo_url, token)
            workspace_path = tempfile.mkdtemp(prefix="bean_onboarding_confirm_")
            self._git_service.copy(cache_path, workspace_path)
            return OnboardingService.confirm_setup(
                workspace_path,
                operation=operation,
                expected_head_sha=expected_head_sha,
                repo_url=repo_url,
                git_service=self._git_service,
                token=token,
                entry_path=entry_path,
                sidecar_main_path=sidecar_main_path,
                sidecar_write_dir=sidecar_write_dir,
            )
        except GitServiceError as e:
            code = _git_error_code(e)
            return {
                "status": "error",
                "code": code,
                "message": _safe_git_message(code),
            }
        finally:
            if workspace_path:
                self._git_service.destroy(workspace_path)


def _safe_git_message(code: str) -> str:
    if code == "REPO_AUTH_FAILED":
        return "Repository authorization failed"
    if code == "REPO_UNREACHABLE":
        return "Repository is unreachable"
    return "Repository setup request failed"
