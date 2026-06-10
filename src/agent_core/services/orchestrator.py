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
import os
import time
from typing import AsyncGenerator

from .ledger import LedgerService
from .preflight import PreflightService, SetupRequiredError
from .workspace import GitService, GitServiceError

logger = logging.getLogger(__name__)


class OrchestratorError(Exception):
    """Unrecoverable orchestration error."""


class AgentOrchestrator:
    """Full lifecycle orchestrator for agent requests.

    Creates a per-request LedgerService that handles write operations
    with preview→confirm split (confirm always re-validates internally).

    Usage from API layer:
        orchestrator = AgentOrchestrator(agent)
        async for chunk in orchestrator.run(request):
            yield format_sse(chunk)
    """

    def __init__(self, agent):
        """agent: PersonalFinanceAgent instance (from agent.py)."""
        self._agent = agent

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
        use_cache: bool = True,
    ) -> AsyncGenerator[dict, None]:
        """Run the full agent lifecycle and yield SSE chunks."""
        start_time = time.monotonic()

        try:
            logger.info(
                "orchestrator setup user_id=%s request_id=%s repo=%s",
                user_id, request_id, repo_url,
            )

            if use_cache and token:
                cache_path = GitService.cache_path(repo_url)
                GitService.ensure_cached(repo_url, token)
                GitService.copy(cache_path, workspace_path)
            else:
                os.makedirs(workspace_path, exist_ok=True)
                GitService.clone(workspace_path, repo_url, token)

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
                token=token,
                whitelist=whitelist,
            ):
                yield chunk

        except GitServiceError as e:
            logger.exception("Git error during orchestration")
            code = (
                "REPO_AUTH_FAILED"
                if "auth" in str(e).lower()
                else "REPO_UNREACHABLE"
            )
            yield {"type": "fatal", "code": code, "message": str(e)}

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
            GitService.destroy(workspace_path)

    async def run_stats(
        self,
        *,
        workspace_path: str,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
        tag: str,
    ) -> dict:
        """Run a lightweight stats query (no LLM)."""
        start_time = time.monotonic()

        try:
            GitService.clone(workspace_path, repo_url, token)

            tag_clean = tag.lstrip("#")
            bql = (
                f'SELECT account, sum(position) AS total '
                f'WHERE tags("{tag_clean}") GROUP BY account ORDER BY total DESC'
            )
            rows, error = LedgerService.Beancount.run_bql_rows(workspace_path, bql)
            if error:
                bql = (
                    f'SELECT account, sum(position) AS total '
                    f'WHERE narration ~ "{tag}" GROUP BY account ORDER BY total DESC'
                )
                rows, error = LedgerService.Beancount.run_bql_rows(
                    workspace_path, bql,
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

        except GitServiceError as e:
            code = (
                "REPO_AUTH_FAILED"
                if "auth" in str(e).lower()
                else "REPO_UNREACHABLE"
            )
            return {"status": "error", "error": {"code": code, "message": str(e)}}

        finally:
            GitService.destroy(workspace_path)

    async def run_accounts(
        self,
        *,
        workspace_path: str,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
    ) -> dict:
        """Run account listing (no LLM)."""
        start_time = time.monotonic()

        try:
            GitService.clone(workspace_path, repo_url, token)

            if not PreflightService.check_setup(workspace_path):
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

            accounts = PreflightService.list_accounts(workspace_path)
            raw = PreflightService.get_raw_open_directives(workspace_path)
            return {
                "status": "ok",
                "accounts": accounts,
                "raw_accounts": raw,
                "usage": {
                    "duration_ms": int((time.monotonic() - start_time) * 1000),
                },
            }

        except GitServiceError as e:
            code = (
                "REPO_AUTH_FAILED"
                if "auth" in str(e).lower()
                else "REPO_UNREACHABLE"
            )
            return {"status": "error", "error": {"code": code, "message": str(e)}}

        finally:
            GitService.destroy(workspace_path)
