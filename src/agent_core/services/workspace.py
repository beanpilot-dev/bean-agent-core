"""GitService — deterministic Git operations for ledger workspaces.

Handles clone, pull, fetch, push, commit, and cleanup. Token is
configured via GIT_ASKPASS to prevent credential leaks into .git/config.
"""

import hashlib
import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class GitServiceError(Exception):
    """Unrecoverable Git operation failure."""


class RepoUnreachableError(GitServiceError):
    """Repository not found or network error."""


class RepoAuthFailedError(GitServiceError):
    """Token is invalid or expired."""


class PushRejectedError(GitServiceError):
    """Push failed — remote conflict or permission issue."""


def _workspace_cache_path(repo_url: str) -> str:
    """Derive a stable cache path from the repo URL."""
    repo_hash = hashlib.sha256(repo_url.encode()).hexdigest()[:16]
    return f"/tmp/bean_cache/{repo_hash}"


class GitService:
    """Deterministic Git operations for ledger workspaces.

    All methods accept token and repo_url explicitly — no ContextVar dependency.
    """

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _run_git(args: list[str], cwd: str) -> tuple[int, str, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        result = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, env=env
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    @staticmethod
    def _configure_git_askpass(workspace: str, token: str) -> Path:
        """Write a GIT_ASKPASS helper script so the token never touches URL or .git/config."""
        askpass = Path(workspace) / ".git-askpass"
        askpass.write_text(f"#!/bin/sh\necho '{token}'\n")
        askpass.chmod(0o700)
        os.environ["GIT_ASKPASS"] = str(askpass)
        return askpass

    @staticmethod
    def _configure_git_identity(workspace: str) -> None:
        subprocess.run(
            ["git", "config", "user.email", "agent@local"],
            cwd=workspace, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Finance Agent"],
            cwd=workspace, capture_output=True,
        )

    @staticmethod
    def _cleanup_askpass(askpass: Path | None) -> None:
        if askpass and askpass.exists():
            askpass.unlink(missing_ok=True)

    @staticmethod
    def _classify_clone_error(stderr: str) -> GitServiceError:
        msg = stderr.lower()
        if "not found" in msg:
            return RepoUnreachableError(stderr.strip())
        if "auth" in msg or "401" in msg or "403" in msg or "credential" in msg:
            return RepoAuthFailedError(stderr.strip())
        if "could not resolve" in msg or "unable to access" in msg:
            return RepoUnreachableError(stderr.strip())
        return RepoUnreachableError(stderr.strip())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def cache_path(repo_url: str) -> str:
        """Return the stable cache path for a repo URL."""
        return _workspace_cache_path(repo_url)

    @staticmethod
    def clone(workspace: str, repo_url: str, token: str | None = None) -> str:
        """Clone repo_url into workspace.

        Returns a status string: "CLONED" or raises GitServiceError.
        """
        os.makedirs(workspace, exist_ok=True)
        askpass = None

        try:
            if token:
                askpass = GitService._configure_git_askpass(workspace, token)

            logger.info("Cloning %s into %s", repo_url, workspace)
            rc, _, err = GitService._run_git(["git", "clone", repo_url, "."], cwd=workspace)
            if rc != 0:
                raise GitService._classify_clone_error(err)

            GitService._configure_git_identity(workspace)
            logger.info("Clone complete: %s", workspace)
            return "CLONED"
        finally:
            GitService._cleanup_askpass(askpass)

    @staticmethod
    def ensure_workspace(
        workspace: str, repo_url: str, token: str | None = None
    ) -> str:
        """Ensure workspace is a valid git repo and up to date.

        - If no .git: clone from repo_url.
        - If .git exists: git pull (fetch + merge, non-destructive).

        Returns a status string: CLONED, PULLED:..., or raises GitServiceError.
        """
        is_git_repo = os.path.isdir(os.path.join(workspace, ".git"))

        if not is_git_repo:
            return GitService.clone(workspace, repo_url, token)

        # Pull latest
        logger.info("Pulling latest for %s", workspace)
        askpass = None
        try:
            if token:
                askpass = GitService._configure_git_askpass(workspace, token)
            rc, out, err = GitService._run_git(
                ["git", "pull", "--ff-only"], cwd=workspace
            )
            if rc != 0:
                logger.warning("git pull failed: %s", err)
                return f"PULL_FAILED: {err}"
            return f"PULLED: {out or 'already up to date'}"
        finally:
            GitService._cleanup_askpass(askpass)

    @staticmethod
    def fetch_reset(workspace: str, token: str | None = None) -> str:
        """Fetch origin and reset hard to origin/HEAD. Fast cache refresh.

        Returns status string or raises GitServiceError.
        """
        askpass = None
        try:
            if token:
                askpass = GitService._configure_git_askpass(workspace, token)

            rc, _, err = GitService._run_git(
                ["git", "fetch", "origin"], cwd=workspace
            )
            if rc != 0:
                raise GitService._classify_clone_error(err)

            rc2, _, err2 = GitService._run_git(
                ["git", "reset", "--hard", "origin/HEAD"], cwd=workspace
            )
            if rc2 != 0:
                raise GitServiceError(f"git reset failed: {err2}")

            return "REFRESHED"
        finally:
            GitService._cleanup_askpass(askpass)

    @staticmethod
    def push(workspace: str, token: str | None = None) -> str:
        """Push committed changes. Returns status string."""
        askpass = None
        try:
            if token:
                askpass = GitService._configure_git_askpass(workspace, token)
            rc, out, err = GitService._run_git(
                ["git", "push"], cwd=workspace
            )
            if rc != 0:
                raise PushRejectedError(f"Push failed: {err}")
            status = out or "ok"
            logger.info("Push: %s", status)
            return f"PUSHED: {status}"
        finally:
            GitService._cleanup_askpass(askpass)

    @staticmethod
    def commit_and_push(
        workspace: str, message: str, token: str | None = None
    ) -> dict:
        """Stage data/, commit, and push. Returns result dict.

        Result keys:
            ok (bool)           — True if commit succeeded
            error (str|None)    — error message if ok=False
            push (str|None)     — push status (PUSHED:... or PUSH_FAILED:...)
        """
        subprocess.run(
            ["git", "add", "data/"],
            cwd=workspace, capture_output=True, text=True,
        )
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=workspace, capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.error("git commit failed: %s", result.stderr.strip())
            return {"ok": False, "error": result.stderr.strip(), "push": None}

        logger.info("git commit ok: %s", message)
        try:
            push_status = GitService.push(workspace, token=token)
        except PushRejectedError as e:
            logger.warning("git push failed: %s", e)
            return {"ok": True, "error": None, "push": f"PUSH_FAILED: {e}"}

        return {"ok": True, "error": None, "push": push_status}

    @staticmethod
    def destroy(workspace: str) -> None:
        """Remove workspace directory tree. Graceful — ignores missing paths."""
        shutil.rmtree(workspace, ignore_errors=True)

    @staticmethod
    def copy(workspace: str, target: str) -> None:
        """Copy workspace to target path (for write isolation)."""
        if os.path.exists(target):
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(workspace, target, symlinks=True)

    @staticmethod
    def ensure_cached(repo_url: str, token: str | None = None) -> str:
        """Ensure a stable cache exists at cache_path(repo_url).

        Returns the cache path. On first call: full clone. Subsequent calls:
        git fetch + reset --hard.
        """
        cache = _workspace_cache_path(repo_url)
        if os.path.isdir(os.path.join(cache, ".git")):
            GitService.fetch_reset(cache, token)
        else:
            if os.path.exists(cache):
                shutil.rmtree(cache, ignore_errors=True)
            GitService.clone(cache, repo_url, token)
        return cache
