"""Parity tests for focused operation orchestration."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_core.services.operations.cache import CacheWarmupOperationHandler
from agent_core.services.operations.chat import ChatOperationHandler
from agent_core.services.operations.ledger_reads import LedgerReadOperationHandler
from agent_core.services.operations.lifecycle import (
    PreflightMode,
    RequestWorkspaceLifecycle,
    WorkspaceCacheBusyError,
    WorkspaceGitError,
    WorkspaceSetupRequiredError,
)
from agent_core.services.operations.onboarding import OnboardingOperationHandler
from agent_core.services.types import PreflightResult
from agent_core.services.workspace import (
    CacheLockTimeoutError,
    RepoAuthFailedError,
)


def _lifecycle(
    tmp_path: Path,
    *,
    cache: Mock | None = None,
    git: Mock | None = None,
) -> tuple[RequestWorkspaceLifecycle, Mock, Mock, Path]:
    cache = cache or Mock()
    git = git or Mock()
    cache.acquire.return_value = str(tmp_path / "cache")
    workspace = tmp_path / "request"
    lifecycle = RequestWorkspaceLifecycle(
        cache,
        git,
        workspace_factory=lambda _prefix: str(workspace),
    )
    return lifecycle, cache, git, workspace


def test_request_workspace_lifecycle_cleans_successful_workspace(tmp_path: Path) -> None:
    lifecycle, cache, git, workspace = _lifecycle(tmp_path)

    with lifecycle.open(
        repo_url="repo",
        token="token",
        user_id="user",
        prefix="request_",
    ) as prepared:
        assert prepared.path == str(workspace)

    git.validate_request_credentials.assert_called_once_with("repo", "token")
    cache.acquire.assert_called_once_with("user", "repo", "token")
    git.copy.assert_called_once_with(str(tmp_path / "cache"), str(workspace))
    git.destroy.assert_called_once_with(str(workspace))


def test_request_workspace_lifecycle_maps_and_cleans_setup_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.services.operations import lifecycle as lifecycle_module

    lifecycle, _, git, workspace = _lifecycle(tmp_path)
    monkeypatch.setattr(
        lifecycle_module.PreflightService,
        "validate",
        Mock(side_effect=lifecycle_module.SetupRequiredError("setup required")),
    )

    with pytest.raises(WorkspaceSetupRequiredError, match="setup required"):
        with lifecycle.open(
            repo_url="repo",
            token="token",
            user_id="user",
            prefix="request_",
            preflight_mode=PreflightMode.VALIDATE,
        ):
            pytest.fail("setup-required workspace must not reach the operation")

    git.destroy.assert_called_once_with(str(workspace))


def test_request_workspace_lifecycle_maps_auth_and_cache_failures(tmp_path: Path) -> None:
    auth_git = Mock()
    auth_git.validate_request_credentials.side_effect = RepoAuthFailedError("auth failed")
    auth_lifecycle, _, _, _ = _lifecycle(tmp_path, git=auth_git)

    with pytest.raises(WorkspaceGitError) as auth_error:
        with auth_lifecycle.open(
            repo_url="repo",
            token="token",
            user_id="user",
            prefix="request_",
        ):
            pytest.fail("auth failure must not reach the operation")
    assert auth_error.value.code == "REPO_AUTH_FAILED"

    busy_cache = Mock()
    busy_cache.acquire.side_effect = CacheLockTimeoutError("cache busy")
    busy_lifecycle, _, _, _ = _lifecycle(tmp_path, cache=busy_cache)
    with pytest.raises(WorkspaceCacheBusyError, match="cache busy"):
        with busy_lifecycle.open(
            repo_url="repo",
            token="token",
            user_id="user",
            prefix="request_",
        ):
            pytest.fail("cache failure must not reach the operation")


@pytest.mark.asyncio
async def test_chat_handler_preserves_stream_event_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.services.operations import lifecycle as lifecycle_module

    lifecycle, _, git, workspace = _lifecycle(tmp_path)
    monkeypatch.setattr(
        lifecycle_module.PreflightService,
        "validate",
        Mock(return_value=PreflightResult(status="CLEAN", accounts=["Assets:Cash"])),
    )
    agent = Mock()

    async def stream(**_kwargs):
        yield {
            "is_task_complete": True,
            "require_user_input": False,
            "content": "done",
        }
        yield {"type": "history_snapshot", "messages": []}

    agent.stream = stream
    handler = ChatOperationHandler(agent, lifecycle, git)

    chunks = [
        chunk
        async for chunk in handler.run(
            workspace_path=str(workspace),
            repo_url="repo",
            token="token",
            agent_run_id="run",
            user_id="user",
            request_id="request",
            api_key="key",
            model="model",
            query="query",
            conversation_meta={},
            messages=[],
        )
    ]

    milestones = [
        (chunk.get("type"), chunk.get("category"), chunk.get("state"))
        for chunk in chunks
    ]
    assert milestones[:7] == [
        ("activity", "run", "started"),
        ("activity", "git", "started"),
        ("processing_state", None, "syncing_workspace"),
        ("activity", "git", "completed"),
        ("activity", "validation", "started"),
        ("processing_state", None, "validating_ledger"),
        ("activity", "validation", "completed"),
    ]
    assert chunks[-1]["type"] == "history_snapshot"
    git.destroy.assert_called_once_with(str(workspace))


@pytest.mark.asyncio
async def test_read_and_warmup_handlers_preserve_failure_envelopes(tmp_path: Path) -> None:
    auth_git = Mock()
    auth_git.validate_request_credentials.side_effect = RepoAuthFailedError("secret")
    auth_lifecycle, _, _, _ = _lifecycle(tmp_path, git=auth_git)
    reads = LedgerReadOperationHandler(auth_lifecycle)

    stats = await reads.run_stats(
        repo_url="repo",
        token="token",
        user_id="user",
        request_id="request",
        tag="#trip",
    )
    assert stats == {
        "status": "error",
        "error": {"code": "REPO_AUTH_FAILED", "message": "secret"},
    }

    busy_cache = Mock()
    busy_cache.acquire.side_effect = CacheLockTimeoutError("secret cache path")
    busy_lifecycle, _, _, _ = _lifecycle(tmp_path, cache=busy_cache)
    warmup = await CacheWarmupOperationHandler(busy_lifecycle).run(
        repo_url="repo",
        token="token",
        user_id="user",
        request_id="request",
    )
    assert warmup["cache_state"] == "busy"
    assert warmup["preflight_status"] == "not_run"
    assert warmup["error"] == {
        "code": "CACHE_BUSY",
        "message": "Workspace cache is busy",
    }


@pytest.mark.asyncio
async def test_onboarding_discovery_skips_runtime_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.services.operations import onboarding as onboarding_module

    lifecycle, _, git, _ = _lifecycle(tmp_path)
    discover = Mock(return_value={"status": "ok", "discovery_status": "clean_repo"})
    monkeypatch.setattr(onboarding_module.OnboardingService, "discover", discover)
    handler = OnboardingOperationHandler(lifecycle, git)

    result = await handler.run_discovery(
        repo_url="repo",
        token="token",
        user_id="user",
        request_id="request",
        entry_path=None,
        expected_head_sha=None,
    )

    assert result == {"status": "ok", "discovery_status": "clean_repo"}
    discover.assert_called_once()
