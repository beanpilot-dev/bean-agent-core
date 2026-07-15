"""Approved pending-action application operation."""

import time

from ..ledger import LedgerService
from ..types import LedgerConfig
from ..workspace import GitService
from .lifecycle import PreflightMode, RequestWorkspaceLifecycle, WorkspaceGitError


class PendingActionOperationHandler:
    """Apply an approved action without invoking the LLM."""

    def __init__(
        self,
        lifecycle: RequestWorkspaceLifecycle,
        git_service: GitService,
    ) -> None:
        self._lifecycle = lifecycle
        self._git_service = git_service

    async def run(
        self,
        *,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
        pending_action: dict,
        ledger_config: LedgerConfig | None = None,
    ) -> dict:
        start_time = time.monotonic()
        try:
            with self._lifecycle.open(
                repo_url=repo_url,
                token=token,
                user_id=user_id,
                prefix="bean_apply_",
                preflight_mode=PreflightMode.VALIDATE,
                ledger_config=ledger_config,
            ) as prepared:
                result = LedgerService().apply_pending_action(
                    prepared.path,
                    pending_action,
                    repo_url,
                    self._git_service,
                    token,
                    ledger_config=ledger_config,
                )
                payload = {
                    "status": result.status,
                    "result": getattr(result, "__dict__", {}),
                    "usage": {
                        "duration_ms": int((time.monotonic() - start_time) * 1000)
                    },
                }
                if result.status in {"APPLIED", "SUCCESS"}:
                    return payload
                return {
                    **payload,
                    "error": {
                        "code": result.status,
                        "message": getattr(
                            result,
                            "error",
                            "Pending action apply failed",
                        ),
                    },
                }
        except WorkspaceGitError as error:
            return {
                "status": "error",
                "error": {"code": error.code, "message": str(error)},
                "usage": {
                    "duration_ms": int((time.monotonic() - start_time) * 1000)
                },
            }
