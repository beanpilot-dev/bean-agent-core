"""Shared request-workspace lifecycle for agent-core operations."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum

from ..beancount import Beancount
from ..preflight import PreflightService, SetupRequiredError
from ..types import LedgerConfig, PreflightResult
from ..workspace import (
    CachedWorkspaceManager,
    CacheLockTimeoutError,
    GitService,
    GitServiceError,
    RepoAuthFailedError,
)


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
        preflight_mode: PreflightMode = PreflightMode.NONE,
        ledger_config: LedgerConfig | None = None,
        workspace_path: str | None = None,
    ) -> Iterator[PreparedWorkspace]:
        """Yield an isolated copy of the latest cached repository state."""

        active_path = workspace_path
        try:
            try:
                self._git_service.validate_request_credentials(repo_url, token)
                cache_path = self._cache_manager.acquire(user_id, repo_url, token)
            except CacheLockTimeoutError as error:
                raise WorkspaceCacheBusyError(str(error)) from error
            except GitServiceError as error:
                raise WorkspaceGitError(git_error_code(error), str(error)) from error

            if active_path is None:
                active_path = self._workspace_factory(prefix)
            self._git_service.copy(cache_path, active_path)

            yield self.preflight(
                active_path,
                mode=preflight_mode,
                ledger_config=ledger_config,
            )
        finally:
            if active_path:
                Beancount.invalidate_workspace(active_path)
                self._git_service.destroy(active_path)

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
                preflight = PreflightService.validate(workspace_path, ledger_config)
        except SetupRequiredError as error:
            raise WorkspaceSetupRequiredError(str(error)) from error
        return PreparedWorkspace(
            path=workspace_path,
            preflight=preflight,
            setup_ready=setup_ready,
        )
