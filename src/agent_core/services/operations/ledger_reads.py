"""Read-only stats and account-listing operation handlers."""

import logging
import time

from ..beancount import Beancount
from ..preflight import PreflightService
from ..types import LedgerConfig
from .lifecycle import (
    PreflightMode,
    RequestWorkspaceLifecycle,
    WorkspaceCacheBusyError,
    WorkspaceGitError,
)

logger = logging.getLogger(__name__)


class LedgerReadOperationHandler:
    """Run deterministic ledger reads in isolated request workspaces."""

    def __init__(self, lifecycle: RequestWorkspaceLifecycle) -> None:
        self._lifecycle = lifecycle

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
        start_time = time.monotonic()

        try:
            with self._lifecycle.open(
                repo_url=repo_url,
                token=token,
                user_id=user_id,
                prefix="bean_stats_",
            ) as prepared:
                tag_clean = tag.lstrip("#")
                bql = (
                    f"SELECT account, sum(position) AS total "
                    f'WHERE tags("{tag_clean}") GROUP BY account ORDER BY total DESC'
                )
                rows, error = Beancount.run_bql_rows(prepared.path, bql, ledger_config)
                if error:
                    bql = (
                        f"SELECT account, sum(position) AS total "
                        f'WHERE narration ~ "{tag}" GROUP BY account ORDER BY total DESC'
                    )
                    rows, error = Beancount.run_bql_rows(
                        prepared.path,
                        bql,
                        ledger_config,
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
                    "usage": {
                        "duration_ms": int((time.monotonic() - start_time) * 1000)
                    },
                }

        except WorkspaceCacheBusyError as error:
            logger.error(
                "Cache lock timeout in run_stats() user_id=%s request_id=%s",
                user_id,
                request_id,
            )
            return {
                "status": "error",
                "error": {"code": "INTERNAL_ERROR", "message": str(error)},
            }

        except WorkspaceGitError as error:
            return {
                "status": "error",
                "error": {"code": error.code, "message": str(error)},
            }

    async def run_accounts(
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
                prefix="bean_accounts_",
                preflight_mode=PreflightMode.CHECK_SETUP,
                ledger_config=ledger_config,
            ) as prepared:
                if not prepared.setup_ready:
                    return {
                        "status": "error",
                        "error": {
                            "code": "SETUP_REQUIRED",
                            "message": "Sidecar include directive is missing.",
                        },
                    }

                accounts = PreflightService.list_accounts(prepared.path, ledger_config)
                raw = PreflightService.get_raw_open_directives(
                    prepared.path,
                    ledger_config,
                )
                return {
                    "status": "ok",
                    "accounts": accounts,
                    "raw_accounts": raw,
                    "usage": {
                        "duration_ms": int((time.monotonic() - start_time) * 1000),
                    },
                }

        except WorkspaceCacheBusyError as error:
            logger.error(
                "Cache lock timeout in run_accounts() user_id=%s request_id=%s",
                user_id,
                request_id,
            )
            return {
                "status": "error",
                "error": {"code": "INTERNAL_ERROR", "message": str(error)},
            }

        except WorkspaceGitError as error:
            return {
                "status": "error",
                "error": {"code": error.code, "message": str(error)},
            }
