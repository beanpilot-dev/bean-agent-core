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
from dataclasses import asdict
from typing import AsyncGenerator

from .activity import ActivityEmitter
from .ledger import Beancount, LedgerService
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


def _processing_state(
    *,
    state: str,
    run_id: str,
    label: str,
    ledger_mutation_state: str = "read_only",
    detail: str | None = None,
    outcome_summary: str | None = None,
    requires_user_action: bool = False,
    require_user_input: bool = False,
    is_task_complete: bool = False,
) -> dict:
    chunk = {
        "type": "processing_state",
        "run_id": run_id,
        "state": state,
        "label": label,
        "ledger_mutation_state": ledger_mutation_state,
        "requires_user_action": requires_user_action,
        "is_task_complete": is_task_complete,
        "require_user_input": require_user_input,
        "content": "",
    }
    if detail:
        chunk["detail"] = detail
    if outcome_summary:
        chunk["outcome_summary"] = outcome_summary
    return chunk


def _working_state_for_query(query: str) -> tuple[str, str]:
    normalized = query.strip().lower()
    write_markers = (
        "record ",
        "add ",
        "create ",
        "log ",
        "bought ",
        "paid ",
        "commit confirmed",
    )
    query_markers = (
        "what ",
        "how ",
        "show ",
        "find ",
        "list ",
        "lookup ",
        "look up ",
        "search ",
        "recent",
        "balance",
        "spend",
        "spent",
        "transaction",
        "?",
    )
    if any(marker in normalized for marker in write_markers):
        return "Preparing a transaction preview", "draft_created"
    if any(marker in normalized for marker in query_markers):
        return "Querying your ledger", "read_only"
    return "Working on your request", "read_only"


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
        """Run the full agent lifecycle and yield SSE chunks."""
        start_time = time.monotonic()
        emitter = ActivityEmitter(run_id=agent_run_id or request_id or "agent-run")
        run_id = agent_run_id or request_id or "agent-run"

        try:
            yield emitter.emit(
                category="run",
                state="started",
                phase="dispatch",
                actor="orchestrator",
                visibility="timeline",
                display_key="agent.run.started",
                fallback_text="Starting the agent run",
            )
            self._git_service.validate_request_credentials(repo_url, token)
            logger.info(
                "orchestrator setup user_id=%s request_id=%s",
                user_id, request_id,
            )

            yield emitter.emit(
                category="git",
                state="started",
                phase="sync",
                actor="orchestrator",
                visibility="details",
                display_key="agent.git.sync_started",
                fallback_text="Preparing the ledger workspace",
            )
            yield _processing_state(
                state="syncing_workspace",
                run_id=run_id,
                label="Syncing your ledger",
            )
            cache_path = self._cache_manager.acquire(user_id, repo_url, token)
            self._git_service.copy(cache_path, workspace_path)
            yield emitter.emit(
                category="git",
                state="completed",
                phase="sync",
                actor="orchestrator",
                visibility="details",
                display_key="agent.git.sync_completed",
                fallback_text="Ledger workspace is ready",
            )

            try:
                yield emitter.emit(
                    category="validation",
                    state="started",
                    phase="preflight",
                    actor="validator",
                    visibility="timeline",
                    display_key="agent.preflight.started",
                    fallback_text="Checking ledger setup",
                )
                yield _processing_state(
                    state="validating_ledger",
                    run_id=run_id,
                    label="Checking ledger health",
                )
                preflight = PreflightService.validate(workspace_path, ledger_config)
                yield emitter.emit(
                    category="validation",
                    state="completed",
                    phase="preflight",
                    actor="validator",
                    visibility="timeline",
                    display_key="agent.preflight.completed",
                    fallback_text="Ledger setup passed validation",
                )
            except SetupRequiredError as e:
                logger.error("Preflight validation failed: SETUP_REQUIRED — %s", e)
                yield emitter.emit(
                    category="validation",
                    state="failed",
                    phase="preflight",
                    actor="validator",
                    visibility="timeline",
                    display_key="agent.preflight.failed",
                    fallback_text="Ledger setup needs attention",
                    safe_detail_summary="Setup is incomplete",
                )
                yield _processing_state(
                    state="failed",
                    run_id=run_id,
                    label="Ledger validation needs attention",
                    ledger_mutation_state="read_only",
                    outcome_summary="No changes were made.",
                    is_task_complete=True,
                )
                yield {"type": "fatal", "code": "SETUP_REQUIRED", "message": str(e)}
                return

            if conversation_meta is None:
                conversation_meta = {}

            whitelist = conversation_meta.get("account_whitelist")
            last_requires_user_input = False
            pending_history_snapshot: dict | None = None
            is_confirm_request = query.strip().lower() == "commit confirmed"
            working_label, working_mutation_state = _working_state_for_query(query)
            yield _processing_state(
                state="applying_changes" if is_confirm_request else "working",
                run_id=run_id,
                label=(
                    "Applying approved changes"
                    if is_confirm_request
                    else working_label
                ),
                ledger_mutation_state=(
                    "applying_changes" if is_confirm_request else working_mutation_state
                ),
            )
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
                ledger_context=asdict(preflight),
                activity_emitter=emitter,
            ):
                if chunk.get("type") == "history_snapshot":
                    pending_history_snapshot = chunk
                    continue
                if chunk.get("require_user_input"):
                    last_requires_user_input = True
                yield chunk
            yield emitter.emit(
                category="run",
                state="awaiting_input" if last_requires_user_input else "completed",
                phase="completed" if not last_requires_user_input else "preview",
                actor="orchestrator",
                visibility="timeline",
                display_key=(
                    "agent.run.awaiting_input"
                    if last_requires_user_input
                    else "agent.run.completed"
                ),
                fallback_text=(
                    "Waiting for your confirmation"
                    if last_requires_user_input
                    else "Agent run completed"
                ),
                display_args={"duration_ms": int((time.monotonic() - start_time) * 1000)},
            )
            if pending_history_snapshot:
                yield pending_history_snapshot

        except CacheLockTimeoutError as e:
            logger.error("Cache lock timeout in run()")
            yield emitter.emit(
                category="run",
                state="failed",
                phase="dispatch",
                actor="orchestrator",
                visibility="timeline",
                display_key="agent.run.failed",
                fallback_text="Agent run failed",
                safe_detail_summary="Workspace cache is busy",
            )
            yield _processing_state(
                state="failed",
                run_id=run_id,
                label="Could not prepare your request",
                ledger_mutation_state="read_only",
                outcome_summary="No changes were made.",
                is_task_complete=True,
            )
            yield {"type": "fatal", "code": "INTERNAL_ERROR", "message": str(e)}

        except GitServiceError as e:
            logger.error(
                "Git error during orchestration code=%s error_type=%s",
                _git_error_code(e),
                type(e).__name__,
            )
            yield emitter.emit(
                category="git",
                state="failed",
                phase="sync",
                actor="orchestrator",
                visibility="timeline",
                display_key="agent.git.sync_failed",
                fallback_text="Could not prepare the ledger workspace",
                safe_detail_summary=_git_error_code(e),
            )
            yield _processing_state(
                state="failed",
                run_id=run_id,
                label="Could not sync your ledger",
                ledger_mutation_state="read_only",
                outcome_summary="No changes were made.",
                is_task_complete=True,
            )
            yield {"type": "fatal", "code": _git_error_code(e), "message": str(e)}

        except Exception as e:
            logger.error("Orchestrator error error_type=%s", type(e).__name__)
            duration_ms = int((time.monotonic() - start_time) * 1000)
            yield emitter.emit(
                category="run",
                state="failed",
                phase="dispatch",
                actor="orchestrator",
                visibility="timeline",
                display_key="agent.run.failed",
                fallback_text="Agent run failed",
                safe_detail_summary=type(e).__name__,
            )
            yield _processing_state(
                state="failed",
                run_id=run_id,
                label="Could not complete your request",
                ledger_mutation_state=(
                    "reverted_or_failed_safely"
                    if query.strip().lower() == "commit confirmed"
                    else "read_only"
                ),
                outcome_summary=(
                    "The request failed safely."
                    if query.strip().lower() == "commit confirmed"
                    else "No changes were made."
                ),
                is_task_complete=True,
            )
            yield {"type": "fatal", "code": "INTERNAL_ERROR", "message": str(e)}
            yield {
                "type": "history_snapshot",
                "messages": messages,
                "trace_id": None,
                "trace_url": None,
                "usage": {"tokens": 0, "duration_ms": duration_ms},
            }

        finally:
            Beancount.invalidate_workspace(workspace_path)
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
                Beancount.invalidate_workspace(workspace_path)
                self._git_service.destroy(workspace_path)

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
        """Apply an approved pending action without invoking the LLM."""
        start_time = time.monotonic()
        workspace_path: str | None = None
        try:
            self._git_service.validate_request_credentials(repo_url, token)
            cache_path = self._cache_manager.acquire(user_id, repo_url, token)
            workspace_path = tempfile.mkdtemp(prefix="bean_apply_")
            self._git_service.copy(cache_path, workspace_path)
            PreflightService.validate(workspace_path, ledger_config)

            result = LedgerService().apply_pending_action(
                workspace_path,
                pending_action,
                repo_url,
                self._git_service,
                token,
                ledger_config=ledger_config,
            )
            payload = {
                "status": result.status,
                "result": getattr(result, "__dict__", {}),
                "usage": {"duration_ms": int((time.monotonic() - start_time) * 1000)},
            }
            if result.status in {"APPLIED", "SUCCESS"}:
                return payload
            return {
                **payload,
                "error": {
                    "code": result.status,
                    "message": getattr(result, "error", "Pending action apply failed"),
                },
            }
        except GitServiceError as e:
            return {
                "status": "error",
                "error": {"code": _git_error_code(e), "message": str(e)},
                "usage": {"duration_ms": int((time.monotonic() - start_time) * 1000)},
            }
        finally:
            if workspace_path:
                Beancount.invalidate_workspace(workspace_path)
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
                Beancount.invalidate_workspace(workspace_path)
                self._git_service.destroy(workspace_path)

    async def run_cache_warmup(
        self,
        *,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
        ledger_config: LedgerConfig | None = None,
    ) -> dict:
        """Warm the shared repo cache and validate a copied temp workspace."""
        start_time = time.monotonic()
        workspace_path: str | None = None

        try:
            self._git_service.validate_request_credentials(repo_url, token)
            cache_path = self._cache_manager.acquire(user_id, repo_url, token)
            workspace_path = tempfile.mkdtemp(prefix="bean_warmup_")
            self._git_service.copy(cache_path, workspace_path)
            preflight = PreflightService.validate(workspace_path, ledger_config)
            if preflight.status != "CLEAN":
                logger.warning(
                    "Cache warmup preflight failed user_id=%s request_id=%s status=%s",
                    user_id,
                    request_id,
                    preflight.status,
                )
                return {
                    "status": "error",
                    "cache_state": "ready",
                    "preflight_status": "error",
                    "request_id": request_id,
                    "error": {
                        "code": "PREFLIGHT_FAILED",
                        "message": "Ledger preflight failed",
                    },
                    "usage": {
                        "duration_ms": int((time.monotonic() - start_time) * 1000),
                    },
                }
            return {
                "status": "ok",
                "cache_state": "ready",
                "preflight_status": "ok",
                "request_id": request_id,
                "usage": {
                    "duration_ms": int((time.monotonic() - start_time) * 1000),
                },
            }
        except SetupRequiredError:
            logger.warning(
                "Cache warmup preflight requires setup user_id=%s request_id=%s",
                user_id,
                request_id,
            )
            return {
                "status": "error",
                "cache_state": "ready",
                "preflight_status": "setup_required",
                "request_id": request_id,
                "error": {
                    "code": "SETUP_REQUIRED",
                    "message": "Workspace ledger setup is incomplete",
                },
                "usage": {
                    "duration_ms": int((time.monotonic() - start_time) * 1000),
                },
            }
        except CacheLockTimeoutError:
            logger.warning(
                "Cache warmup lock timeout user_id=%s request_id=%s",
                user_id,
                request_id,
            )
            return {
                "status": "error",
                "cache_state": "busy",
                "preflight_status": "not_run",
                "request_id": request_id,
                "error": {
                    "code": "CACHE_BUSY",
                    "message": "Workspace cache is busy",
                },
                "usage": {
                    "duration_ms": int((time.monotonic() - start_time) * 1000),
                },
            }
        except GitServiceError as e:
            code = _git_error_code(e)
            logger.warning(
                "Cache warmup git failed user_id=%s request_id=%s code=%s error_type=%s",
                user_id,
                request_id,
                code,
                type(e).__name__,
            )
            return {
                "status": "error",
                "cache_state": "unavailable",
                "preflight_status": "not_run",
                "request_id": request_id,
                "error": {
                    "code": code,
                    "message": _safe_git_message(code),
                },
                "usage": {
                    "duration_ms": int((time.monotonic() - start_time) * 1000),
                },
            }
        finally:
            if workspace_path:
                Beancount.invalidate_workspace(workspace_path)
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
                Beancount.invalidate_workspace(workspace_path)
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
                Beancount.invalidate_workspace(workspace_path)
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
                Beancount.invalidate_workspace(workspace_path)
                self._git_service.destroy(workspace_path)


def _safe_git_message(code: str) -> str:
    if code == "REPO_AUTH_FAILED":
        return "Repository authorization failed"
    if code == "REPO_UNREACHABLE":
        return "Repository is unreachable"
    return "Repository setup request failed"
