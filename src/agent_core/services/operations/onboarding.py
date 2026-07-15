"""Deterministic onboarding operation handlers."""

import logging

from ..onboarding import OnboardingService, SetupOperation
from ..workspace import GitService
from .cache import safe_git_message
from .lifecycle import (
    RequestWorkspaceLifecycle,
    WorkspaceCacheBusyError,
    WorkspaceGitError,
)

logger = logging.getLogger(__name__)


class OnboardingOperationHandler:
    """Run discovery and setup operations without normal ledger preflight."""

    def __init__(
        self,
        lifecycle: RequestWorkspaceLifecycle,
        git_service: GitService,
    ) -> None:
        self._lifecycle = lifecycle
        self._git_service = git_service

    async def run_discovery(
        self,
        *,
        repo_url: str,
        token: str | None,
        user_id: str,
        request_id: str | None,
        entry_path: str | None,
        expected_head_sha: str | None,
    ) -> dict:
        try:
            with self._lifecycle.open(
                repo_url=repo_url,
                token=token,
                user_id=user_id,
                prefix="bean_onboarding_discover_",
            ) as prepared:
                return OnboardingService.discover(
                    prepared.path,
                    entry_path=entry_path,
                    expected_head_sha=expected_head_sha,
                )
        except WorkspaceCacheBusyError:
            logger.error(
                "Cache lock timeout in run_onboarding_discovery() "
                "user_id=%s request_id=%s",
                user_id,
                request_id,
            )
            return {
                "status": "error",
                "discovery_status": "invalid_request",
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Onboarding discovery is unavailable",
                },
            }
        except WorkspaceGitError as error:
            return {
                "status": "error",
                "discovery_status": (
                    "repo_auth_failed"
                    if error.code == "REPO_AUTH_FAILED"
                    else "repo_unreachable"
                ),
                "error": {
                    "code": error.code,
                    "message": safe_git_message(error.code),
                },
            }

    async def run_setup_preview(
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
        try:
            with self._lifecycle.open(
                repo_url=repo_url,
                token=token,
                user_id=user_id,
                prefix="bean_onboarding_preview_",
            ) as prepared:
                return OnboardingService.preview_setup(
                    prepared.path,
                    operation=operation,
                    entry_path=entry_path,
                    sidecar_main_path=sidecar_main_path,
                    sidecar_write_dir=sidecar_write_dir,
                    ledger_title=ledger_title,
                    operating_currency=operating_currency,
                )
        except WorkspaceGitError as error:
            return {
                "status": "error",
                "code": error.code,
                "message": safe_git_message(error.code),
            }

    async def run_setup_confirm(
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
        try:
            with self._lifecycle.open(
                repo_url=repo_url,
                token=token,
                user_id=user_id,
                prefix="bean_onboarding_confirm_",
            ) as prepared:
                return OnboardingService.confirm_setup(
                    prepared.path,
                    operation=operation,
                    expected_head_sha=expected_head_sha,
                    repo_url=repo_url,
                    git_service=self._git_service,
                    token=token,
                    entry_path=entry_path,
                    sidecar_main_path=sidecar_main_path,
                    sidecar_write_dir=sidecar_write_dir,
                    ledger_title=ledger_title,
                    operating_currency=operating_currency,
                )
        except WorkspaceGitError as error:
            return {
                "status": "error",
                "code": error.code,
                "message": safe_git_message(error.code),
            }
