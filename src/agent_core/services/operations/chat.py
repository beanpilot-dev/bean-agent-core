"""Chat streaming operation over an isolated ledger workspace."""

import logging
import time
from dataclasses import asdict
from typing import AsyncGenerator

from ..activity import ActivityEmitter
from ..types import LedgerConfig
from ..workspace import GitService
from .lifecycle import (
    PreflightMode,
    RequestWorkspaceLifecycle,
    WorkspaceCacheBusyError,
    WorkspaceGitError,
    WorkspaceSetupRequiredError,
)

logger = logging.getLogger(__name__)


def processing_state(
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


class ChatOperationHandler:
    """Own chat preflight, agent streaming, and protocol-event translation."""

    def __init__(
        self,
        agent,
        lifecycle: RequestWorkspaceLifecycle,
        git_service: GitService,
    ) -> None:
        self._agent = agent
        self._lifecycle = lifecycle
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
            logger.info(
                "orchestrator setup user_id=%s request_id=%s",
                user_id,
                request_id,
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
            yield processing_state(
                state="syncing_workspace",
                run_id=run_id,
                label="Syncing your ledger",
            )

            with self._lifecycle.open(
                repo_url=repo_url,
                token=token,
                user_id=user_id,
                prefix="bean_workspace_",
                preflight_mode=PreflightMode.NONE,
                ledger_config=ledger_config,
                workspace_path=workspace_path,
            ) as prepared:
                yield emitter.emit(
                    category="git",
                    state="completed",
                    phase="sync",
                    actor="orchestrator",
                    visibility="details",
                    display_key="agent.git.sync_completed",
                    fallback_text="Ledger workspace is ready",
                )
                yield emitter.emit(
                    category="validation",
                    state="started",
                    phase="preflight",
                    actor="validator",
                    visibility="timeline",
                    display_key="agent.preflight.started",
                    fallback_text="Checking ledger setup",
                )
                yield processing_state(
                    state="validating_ledger",
                    run_id=run_id,
                    label="Checking ledger health",
                )
                prepared = self._lifecycle.preflight(
                    prepared.path,
                    mode=PreflightMode.VALIDATE,
                    ledger_config=ledger_config,
                )
                preflight = prepared.preflight
                if preflight is None:
                    raise RuntimeError("validated workspace is missing preflight context")
                yield emitter.emit(
                    category="validation",
                    state="completed",
                    phase="preflight",
                    actor="validator",
                    visibility="timeline",
                    display_key="agent.preflight.completed",
                    fallback_text="Ledger setup passed validation",
                )

                if conversation_meta is None:
                    conversation_meta = {}

                whitelist = conversation_meta.get("account_whitelist")
                last_requires_user_input = False
                pending_history_snapshot: dict | None = None
                yield processing_state(
                    state="working",
                    run_id=run_id,
                    label="Working on your request",
                    ledger_mutation_state="read_only",
                )
                async for chunk in self._agent.stream(
                    query=query,
                    prior=messages,
                    conversation_meta=conversation_meta,
                    api_key=api_key,
                    model=model,
                    workspace=prepared.path,
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
                    display_args={
                        "duration_ms": int((time.monotonic() - start_time) * 1000)
                    },
                )
                if pending_history_snapshot:
                    yield pending_history_snapshot

        except WorkspaceSetupRequiredError as error:
            logger.error("Preflight validation failed: SETUP_REQUIRED — %s", error)
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
            yield processing_state(
                state="failed",
                run_id=run_id,
                label="Ledger validation needs attention",
                ledger_mutation_state="read_only",
                outcome_summary="No changes were made.",
                is_task_complete=True,
            )
            yield {"type": "fatal", "code": "SETUP_REQUIRED", "message": str(error)}

        except WorkspaceCacheBusyError as error:
            logger.error(
                "Cache lock timeout in run() user_id=%s request_id=%s",
                user_id,
                request_id,
            )
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
            yield processing_state(
                state="failed",
                run_id=run_id,
                label="Could not prepare your request",
                ledger_mutation_state="read_only",
                outcome_summary="No changes were made.",
                is_task_complete=True,
            )
            yield {"type": "fatal", "code": "INTERNAL_ERROR", "message": str(error)}

        except WorkspaceGitError as error:
            logger.error(
                "Git error during orchestration code=%s error_type=%s",
                error.code,
                type(error.__cause__).__name__,
            )
            yield emitter.emit(
                category="git",
                state="failed",
                phase="sync",
                actor="orchestrator",
                visibility="timeline",
                display_key="agent.git.sync_failed",
                fallback_text="Could not prepare the ledger workspace",
                safe_detail_summary=error.code,
            )
            yield processing_state(
                state="failed",
                run_id=run_id,
                label="Could not sync your ledger",
                ledger_mutation_state="read_only",
                outcome_summary="No changes were made.",
                is_task_complete=True,
            )
            yield {"type": "fatal", "code": error.code, "message": str(error)}

        except Exception as error:
            logger.error("Orchestrator error error_type=%s", type(error).__name__)
            duration_ms = int((time.monotonic() - start_time) * 1000)
            yield emitter.emit(
                category="run",
                state="failed",
                phase="dispatch",
                actor="orchestrator",
                visibility="timeline",
                display_key="agent.run.failed",
                fallback_text="Agent run failed",
                safe_detail_summary=type(error).__name__,
            )
            yield processing_state(
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
            yield {"type": "fatal", "code": "INTERNAL_ERROR", "message": str(error)}
            yield {
                "type": "history_snapshot",
                "messages": messages,
                "trace_id": None,
                "trace_url": None,
                "usage": {"tokens": 0, "duration_ms": duration_ms},
            }
