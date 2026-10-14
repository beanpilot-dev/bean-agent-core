"""Shared request-workspace lifecycle for agent-core operations."""

import asyncio
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from enum import Enum

from ..beancount import Beancount
from ..plugin_policy import RepositoryPluginRejectedError, enforce_plugin_policy
from ..preflight import PreflightService, SetupRequiredError
from ..timing import stage_timer
from ..types import LedgerConfig, PreflightResult
from ..workspace import (
    CachedWorkspaceManager,
    CacheLockTimeoutError,
    GitService,
    GitServiceError,
    RepoAuthFailedError,
)
from .blocking import run_blocking


class PreflightMode(str, Enum):
    """Workspace validation required before an operation runs."""

    NONE = "none"
    CHECK_SETUP = "check_setup"
    VALIDATE = "validate"


class RequestWorkspaceError(Exception):
    """Typed failure while preparing an isolated request workspace."""


class WorkspaceCacheBusyError(RequestWorkspaceError):
    """The shared repository cache could not be acquired in time."""


class WorkspaceSetupRequiredError(RequestWorkspaceError):
    """The copied ledger workspace requires deterministic setup."""


class WorkspaceGitError(RequestWorkspaceError):
    """A repository credential or availability failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class WorkspacePluginError(RequestWorkspaceError):
    """The repository contains executable Beancount plugin configuration."""


@dataclass(frozen=True)
class PreparedWorkspace:
    """An isolated workspace plus any requested preflight result."""

    path: str
    preflight: PreflightResult | None = None
    setup_ready: bool | None = None


def git_error_code(error: GitServiceError) -> str:
    """Map concrete Git failures to the stable public error catalog."""

    return "REPO_AUTH_FAILED" if isinstance(error, RepoAuthFailedError) else "REPO_UNREACHABLE"


class RequestWorkspaceLifecycle:
    """Prepare, validate, and always clean an isolated request workspace."""

    def __init__(
        self,
        cache_manager: CachedWorkspaceManager,
        git_service: GitService,
        workspace_factory: Callable[[str], str],
    ) -> None:
        self._cache_manager = cache_manager
        self._git_service = git_service
        self._workspace_factory = workspace_factory

    @contextmanager
    def open(
        self,
        *,
        repo_url: str,
        token: str | None,
        user_id: str,
        prefix: str,
        branch: str | None = None,
        preflight_mode: PreflightMode = PreflightMode.NONE,
        ledger_config: LedgerConfig | None = None,
        workspace_path: str | None = None,
        read_recent: bool = False,
        admitted_at: float | None = None,
    ) -> Iterator[PreparedWorkspace]:
        """Yield an isolated copy of the latest cached repository state."""

        active_path = workspace_path
        writing = prefix.startswith(("bean_apply_", "bean_onboarding_confirm_"))
        invalidate = getattr(type(self._cache_manager), "invalidate", None)
        if writing and invalidate is not None:
            invalidate(self._cache_manager, user_id, repo_url, token, branch)
        try:
            try:
                self._git_service.validate_request_credentials(repo_url, token)
                if active_path is None:
                    active_path = self._workspace_factory(prefix)
                snapshot = getattr(type(self._cache_manager), "acquire_and_copy", None)
                if snapshot is not None:
                    snapshot_options = (
                        {"read_recent": True, "admitted_at": admitted_at} if read_recent else {}
                    )
                    with stage_timer("workspace_acquisition"):
                        snapshot(
                            self._cache_manager,
                            user_id,
                            repo_url,
                            token,
                            active_path,
                            branch,
                            **snapshot_options,
                        )
                    cache_path = None
                else:
                    cache_path = (
                        self._cache_manager.acquire(user_id, repo_url, token)
                        if branch is None
                        else self._cache_manager.acquire(user_id, repo_url, token, branch)
                    )
            except CacheLockTimeoutError as error:
                raise WorkspaceCacheBusyError(str(error)) from error
            except GitServiceError as error:
                raise WorkspaceGitError(git_error_code(error), str(error)) from error

            if cache_path is not None:
                with stage_timer("workspace_copy"):
                    self._git_service.copy(cache_path, active_path)

            try:
                enforce_plugin_policy(active_path, ledger_config)
            except RepositoryPluginRejectedError as error:
                raise WorkspacePluginError(str(error)) from error

            yield self.preflight(
                active_path,
                mode=preflight_mode,
                ledger_config=ledger_config,
            )
        finally:
            try:
                if writing and invalidate is not None:
                    invalidate(self._cache_manager, user_id, repo_url, token, branch)
            finally:
                if active_path:
                    Beancount.invalidate_workspace(active_path)
                    self._git_service.destroy(active_path)

    @asynccontextmanager
    async def open_async(self, **kwargs):
        """Open a workspace off-loop and retain ownership through async use."""

        kwargs.setdefault("admitted_at", time.time())
        manager = self.open(**kwargs)
        entering = asyncio.create_task(run_blocking(manager.__enter__))
        cancelled = False
        while not entering.done():
            try:
                await asyncio.wait({entering})
            except asyncio.CancelledError:
                cancelled = True
        if entering.cancelled() or entering.exception() is not None:
            if cancelled:
                # __enter__ already closed its generator and cleaned up.
                raise asyncio.CancelledError
            entering.result()
        prepared = entering.result()
        if cancelled:
            await _finish_workspace(manager, (None, None, None))
            raise asyncio.CancelledError
        try:
            yield prepared
        except BaseException:
            await _finish_workspace(manager, sys.exc_info())
            raise
        else:
            await _finish_workspace(manager, (None, None, None))

    @staticmethod
    def preflight(
        workspace_path: str,
        *,
        mode: PreflightMode,
        ledger_config: LedgerConfig | None = None,
    ) -> PreparedWorkspace:
        """Run the selected deterministic preflight inside a prepared workspace."""

        preflight: PreflightResult | None = None
        setup_ready: bool | None = None
        try:
            if mode is PreflightMode.CHECK_SETUP:
                setup_ready = PreflightService.check_setup(workspace_path, ledger_config)
            elif mode is PreflightMode.VALIDATE:
                with stage_timer("preflight"):
                    preflight = PreflightService.validate(workspace_path, ledger_config)
        except SetupRequiredError as error:
            raise WorkspaceSetupRequiredError(str(error)) from error
        return PreparedWorkspace(
            path=workspace_path,
            preflight=preflight,
            setup_ready=setup_ready,
        )


async def _finish_workspace(manager, exc_info: tuple) -> None:
    """Run lifecycle exit to completion even when cancellation arrives queued."""

    finalizer = asyncio.create_task(run_blocking(manager.__exit__, *exc_info))
    cancelled = False
    while not finalizer.done():
        try:
            await asyncio.wait({finalizer})
        except asyncio.CancelledError:
            cancelled = True
    # Retrieve a worker exception before preserving cancellation.
    try:
        finalizer.result()
    except Exception:
        if not cancelled:
            raise
    if cancelled:
        raise asyncio.CancelledError
