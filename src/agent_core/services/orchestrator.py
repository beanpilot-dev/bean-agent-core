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
import time
from typing import AsyncGenerator

from .ledger import Beancount
from .preflight import PreflightService, SetupRequiredError
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
    ) -> AsyncGenerator[dict, None]:
        """Run the full agent lifecycle and yield SSE chunks."""
        start_time = time.monotonic()

        try:
            self._git_service.validate_request_credentials(repo_url, token)
            logger.info(
                "orchestrator setup user_id=%s request_id=%s repo=%s",
                user_id, request_id, repo_url,
            )

            cache_path = self._cache_manager.acquire(user_id, repo_url, token)
            self._git_service.copy(cache_path, workspace_path)

            try:
                PreflightService.validate(workspace_path)
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
            ):
                yield chunk

        except CacheLockTimeoutError as e:
            logger.error("Cache lock timeout in run(): %s", e)
            yield {"type": "fatal", "code": "INTERNAL_ERROR", "message": str(e)}

        except GitServiceError as e:
            logger.exception("Git error during orchestration")
            yield {"type": "fatal", "code": _git_error_code(e), "message": str(e)}

        except Exception as e:
            logger.exception("Orchestrator error")
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
    ) -> dict:
        """Run a lightweight stats query (no LLM)."""
        start_time = time.monotonic()

        try:
            self._git_service.validate_request_credentials(repo_url, token)
            cache_path = self._cache_manager.acquire(user_id, repo_url, token)

            tag_clean = tag.lstrip("#")
            bql = (
                f'SELECT account, sum(position) AS total '
                f'WHERE tags("{tag_clean}") GROUP BY account ORDER BY total DESC'
            )
            rows, error = Beancount.run_bql_rows(cache_path, bql)
            if error:
                bql = (
                    f'SELECT account, sum(position) AS total '
                    f'WHERE narration ~ "{tag}" GROUP BY account ORDER BY total DESC'
                )
                rows, error = Beancount.run_bql_rows(
                    cache_path, bql,
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
            logger.error("Cache lock timeout in run_stats(): %s", e)
            return {
                "status": "error",
                "error": {"code": "INTERNAL_ERROR", "message": str(e)},
            }

        except GitServiceError as e:
            return {
                "status": "error",
                "error": {"code": _git_error_code(e), "message": str(e)},
            }

    async def run_accounts(
        self,
        *,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
    ) -> dict:
        """Run account listing (no LLM)."""
        start_time = time.monotonic()

        try:
            self._git_service.validate_request_credentials(repo_url, token)
            cache_path = self._cache_manager.acquire(user_id, repo_url, token)

            if not PreflightService.check_setup(cache_path):
                return {
                    "status": "error",
                    "error": {
                        "code": "SETUP_REQUIRED",
                        "message": (
                            "Sidecar include directive is missing. "
                            'Add: include "agent_inc/main.beancount" to '
                            "data/main.beancount"
                        ),
                    },
                }

            accounts = PreflightService.list_accounts(cache_path)
            raw = PreflightService.get_raw_open_directives(cache_path)
            return {
                "status": "ok",
                "accounts": accounts,
                "raw_accounts": raw,
                "usage": {
                    "duration_ms": int((time.monotonic() - start_time) * 1000),
                },
            }

        except CacheLockTimeoutError as e:
            logger.error("Cache lock timeout in run_accounts(): %s", e)
            return {
                "status": "error",
                "error": {"code": "INTERNAL_ERROR", "message": str(e)},
            }

        except GitServiceError as e:
            return {
                "status": "error",
                "error": {"code": _git_error_code(e), "message": str(e)},
            }
