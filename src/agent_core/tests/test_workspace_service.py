"""Unit and integration tests for GitService and CachedWorkspaceManager."""

import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_core.services.workspace import (
    CachedWorkspaceManager,
    CloudGitService,
    GitService,
    LocalGitService,
    PushRejectedError,
    RepoAuthFailedError,
    RepoUnreachableError,
)


def run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_clone_pull_fetch_reset_push_and_commit(tmp_path: Path, bare_ledger_repo: Path) -> None:
    service = LocalGitService(str(bare_ledger_repo))
    clone = tmp_path / "clone"

    assert service.clone(str(clone), "ignored") == "CLONED"
    assert service.ensure_workspace(str(clone), "ignored").startswith("PULLED:")
    assert service.fetch_reset(str(clone), "ignored") == "REFRESHED"

    main = clone / "data" / "main.beancount"
    main.write_text(main.read_text() + "; changed\n")
    result = service.commit_and_push(str(clone), "change", "ignored")

    assert result["ok"] is True
    assert result["push"].startswith("PUSHED:")


def test_clone_classifies_auth_and_network_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = CloudGitService()
    errors = iter(
        [
            (1, "", "fatal: Authentication failed"),
            (1, "", "Could not resolve host"),
        ]
    )
    monkeypatch.setattr(service, "_run_git", lambda *_args, **_kwargs: next(errors))

    with pytest.raises(RepoAuthFailedError):
        service.clone(str(tmp_path / "auth"), "https://example.com/repo.git", "token")
    with pytest.raises(RepoUnreachableError):
        service.clone(str(tmp_path / "network"), "https://example.com/repo.git", "token")


def test_push_rejection_and_commit_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = CloudGitService()
    monkeypatch.setattr(service, "_run_git", lambda *_args, **_kwargs: (1, "", "rejected"))

    with pytest.raises(PushRejectedError):
        service.push(str(tmp_path), "https://example.com/repo.git", "token")

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", "nothing to commit"),
    )
    result = service.commit_and_push(
        str(tmp_path), "message", "https://example.com/repo.git", "token"
    )
    assert result["ok"] is False


def test_copy_and_destroy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / "file").write_text("value")

    GitService.copy(str(source), str(target))
    assert (target / "file").read_text() == "value"
    GitService.destroy(str(target))
    assert not target.exists()


def test_askpass_token_is_not_persisted_in_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    token = "secret-token"

    askpass = CloudGitService._configure_git_askpass(str(workspace), token)
    try:
        assert askpass.parent != workspace
        assert token not in "\n".join(
            path.read_text(errors="ignore") for path in workspace.rglob("*") if path.is_file()
        )
    finally:
        CloudGitService._cleanup_askpass(askpass)

    assert not askpass.exists()


def test_cache_miss_hit_and_user_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    git = Mock()
    git.effective_repo_url.return_value = "repo"
    git._resolve_credentials.return_value = ("repo", None)
    git._run_git.return_value = (0, "up to date", "")
    manager = CachedWorkspaceManager(git, ttl_seconds=900)
    monkeypatch.setattr(manager, "CACHE_ROOT", str(tmp_path / "cache"))

    def clone(path: str, *_args) -> None:
        (Path(path) / ".git").mkdir(parents=True)

    git.clone.side_effect = clone
    first = manager.acquire("user-a", "repo", "token")
    second = manager.acquire("user-a", "repo", "token")
    other = manager.acquire("user-b", "repo", "token")

    assert first == second
    assert first != other
    assert git.clone.call_count == 2
    git._run_git.assert_called_once()


def test_cache_refresh_falls_back_to_fetch_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git = Mock()
    git.effective_repo_url.return_value = "repo"
    git._resolve_credentials.return_value = ("repo", None)
    git._run_git.return_value = (1, "", "not fast-forward")
    manager = CachedWorkspaceManager(git, ttl_seconds=900)
    monkeypatch.setattr(manager, "CACHE_ROOT", str(tmp_path / "cache"))
    key = manager._cache_key("user", "repo")
    cache = Path(manager._cache_path(key))
    (cache / ".git").mkdir(parents=True)
    manager._touch(key)

    manager.acquire("user", "repo")

    git.fetch_reset.assert_called_once_with(str(cache), "repo", None)


def test_cleanup_expired_removes_old_and_orphaned_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = CachedWorkspaceManager(Mock(), ttl_seconds=1)
    monkeypatch.setattr(manager, "CACHE_ROOT", str(tmp_path / "cache"))
    old = Path(manager.CACHE_ROOT) / "old"
    orphan = Path(manager.CACHE_ROOT) / "orphan"
    old.mkdir(parents=True)
    orphan.mkdir()
    meta = Path(f"{old}.meta")
    meta.write_text("old")
    os.utime(meta, (0, 0))

    manager.cleanup_expired()

    assert not old.exists()
    assert not orphan.exists()


def test_active_read_snapshot_is_isolated_from_cache_refresh(
    tmp_path: Path, bare_ledger_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = LocalGitService(str(bare_ledger_repo))
    manager = CachedWorkspaceManager(service, ttl_seconds=900)
    monkeypatch.setattr(manager, "CACHE_ROOT", str(tmp_path / "cache"))
    cache = Path(manager.acquire("user", "ignored"))
    read_snapshot = tmp_path / "read"
    service.copy(str(cache), str(read_snapshot))

    updater = tmp_path / "updater"
    run_git(["clone", str(bare_ledger_repo), str(updater)], tmp_path)
    run_git(["config", "user.email", "test@example.com"], updater)
    run_git(["config", "user.name", "Test"], updater)
    remote_main = updater / "data" / "main.beancount"
    remote_main.write_text(remote_main.read_text() + "; remote-refresh\n")
    run_git(["add", "data/main.beancount"], updater)
    run_git(["commit", "-m", "remote refresh"], updater)
    run_git(["push", "origin", "HEAD"], updater)

    refreshed_cache = Path(manager.acquire("user", "ignored"))

    assert "; remote-refresh" in (refreshed_cache / "data" / "main.beancount").read_text()
    assert "; remote-refresh" not in (read_snapshot / "data" / "main.beancount").read_text()
