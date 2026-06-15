"""Tests for construction-time Git repository mode selection."""

import subprocess

import pytest

from agent_core.services.ledger import _git_dependency_error
from agent_core.services.orchestrator import _git_error_code
from agent_core.services.types import DependencyUnavailable
from agent_core.services.workspace import (
    CloudGitService,
    GitService,
    LocalGitService,
    RepoAuthFailedError,
    RepoUnreachableError,
)


def _run(args: list[str], cwd: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def test_invalid_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="AGENT_MODE"):
        GitService.from_environment("", "")


def test_local_mode_requires_repo_url() -> None:
    with pytest.raises(ValueError, match="LOCAL_REPO_URL"):
        GitService.from_environment("local", "")


def test_cloud_mode_requires_url_and_token() -> None:
    service = CloudGitService()

    with pytest.raises(RepoAuthFailedError, match="URL"):
        service.validate_request_credentials("", "token")
    with pytest.raises(RepoAuthFailedError, match="HTTPS"):
        service.validate_request_credentials("/data/local.git", "token")
    with pytest.raises(RepoAuthFailedError, match="token"):
        service.validate_request_credentials("https://example.com/repo.git", "")


def test_cloud_askpass_returns_username_and_token(tmp_path) -> None:
    askpass = CloudGitService()._configure_git_askpass(str(tmp_path), "oauth-secret")
    try:
        username = subprocess.run(
            [str(askpass), "Username for https://example.com"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        password = subprocess.run(
            [str(askpass), "Password for https://example.com"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    finally:
        CloudGitService._cleanup_askpass(askpass)

    assert username == "oauth2"
    assert password == "oauth-secret"


def test_cloud_clone_keeps_destination_empty_for_git(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = CloudGitService()
    workspace = tmp_path / "clone"

    def fake_run_git(args: list[str], cwd: str, askpass=None):
        assert args[:2] == ["git", "clone"]
        assert list(workspace.iterdir()) == []
        assert askpass is not None
        assert askpass.parent != workspace
        return 0, "", ""

    monkeypatch.setattr(service, "_run_git", fake_run_git)
    monkeypatch.setattr(service, "_configure_git_identity", lambda _workspace: None)

    service.clone(str(workspace), "https://example.com/repo.git", "oauth-secret")


def test_local_mode_clones_configured_repo_without_askpass(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    source.mkdir()
    seed.mkdir()
    _run(["git", "init", "--bare"], str(source))
    _run(["git", "init"], str(seed))
    _run(["git", "config", "user.email", "test@example.com"], str(seed))
    _run(["git", "config", "user.name", "Test"], str(seed))
    (seed / "data").mkdir()
    (seed / "data" / "main.beancount").write_text('option "title" "Test"\n')
    (seed / "README.md").write_text("seed\n")
    _run(["git", "add", "README.md", "data/main.beancount"], str(seed))
    _run(["git", "commit", "-m", "seed"], str(seed))
    _run(["git", "remote", "add", "origin", str(source)], str(seed))
    _run(["git", "push", "origin", "HEAD"], str(seed))

    service = GitService.from_environment("local", str(source))
    assert isinstance(service, LocalGitService)
    monkeypatch.setattr(
        service,
        "_configure_git_askpass",
        lambda *_args: pytest.fail("local mode must not configure GIT_ASKPASS"),
    )

    service.clone(str(clone), "https://ignored.example/repo.git", "ignored-token")

    assert (clone / ".git").is_dir()
    assert (clone / "README.md").read_text() == "seed\n"

    (clone / "data" / "main.beancount").write_text('option "title" "Updated"\n')
    result = service.commit_and_push(str(clone), "update ledger", "", "")

    assert result["ok"] is True
    assert result["push"].startswith("PUSHED:")

    verify = tmp_path / "verify"
    _run(["git", "clone", str(source), str(verify)], str(tmp_path))
    assert (verify / "data" / "main.beancount").read_text() == 'option "title" "Updated"\n'


def test_push_failure_is_dependency_unavailable() -> None:
    result = _git_dependency_error(
        {"ok": True, "error": None, "push": "PUSH_FAILED: permission denied"}
    )

    assert isinstance(result, DependencyUnavailable)
    assert result.retryable is True
    assert "push failed" in result.error.lower()


def test_git_error_codes_use_exception_types() -> None:
    assert _git_error_code(RepoAuthFailedError("missing URL")) == "REPO_AUTH_FAILED"
    assert _git_error_code(RepoUnreachableError("network down")) == "REPO_UNREACHABLE"
