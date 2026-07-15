"""Best-effort repository cache warmup operation."""

import logging
import time

from ..types import LedgerConfig
from .lifecycle import (
    PreflightMode,
    RequestWorkspaceLifecycle,
    WorkspaceCacheBusyError,
    WorkspaceGitError,
    WorkspaceSetupRequiredError,
)

logger = logging.getLogger(__name__)


def safe_git_message(code: str) -> str:
    if code == "REPO_AUTH_FAILED":
        return "Repository authorization failed"
    if code == "REPO_UNREACHABLE":
        return "Repository is unreachable"
    return "Repository setup request failed"


class CacheWarmupOperationHandler:
    """Warm the shared cache and validate a disposable copy."""

    def __init__(self, lifecycle: RequestWorkspaceLifecycle) -> None:
        self._lifecycle = lifecycle

    async def run(
        self,
        *,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
        ledger_config: LedgerConfig | None = None,
    ) -> dict:
        start_time = time.monotonic()

        try:
            with self._lifecycle.open(
                repo_url=repo_url,
                token=token,
                user_id=user_id,
                prefix="bean_warmup_",
                preflight_mode=PreflightMode.VALIDATE,
                ledger_config=ledger_config,
            ) as prepared:
                preflight = prepared.preflight
                if preflight is None:
                    raise RuntimeError("validated workspace is missing preflight context")
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
        except WorkspaceSetupRequiredError:
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
        except WorkspaceCacheBusyError:
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
        except WorkspaceGitError as error:
            logger.warning(
                "Cache warmup git failed user_id=%s request_id=%s code=%s error_type=%s",
                user_id,
                request_id,
                error.code,
                type(error.__cause__).__name__,
            )
            return {
                "status": "error",
                "cache_state": "unavailable",
                "preflight_status": "not_run",
                "request_id": request_id,
                "error": {
                    "code": error.code,
                    "message": safe_git_message(error.code),
                },
                "usage": {
                    "duration_ms": int((time.monotonic() - start_time) * 1000),
                },
            }
