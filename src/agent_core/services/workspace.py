"""GitService — deterministic Git operations for ledger workspaces.

Handles clone, pull, fetch, push, commit, and cleanup. Token is
configured via GIT_ASKPASS to prevent credential leaks into .git/config.
"""

import hashlib
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from filelock import FileLock

logger = logging.getLogger(__name__)


class GitServiceError(Exception):
    """Unrecoverable Git operation failure."""


class RepoUnreachableError(GitServiceError):
    """Repository not found or network error."""


class RepoAuthFailedError(GitServiceError):
    """Token is invalid or expired."""


class PushRejectedError(GitServiceError):
    """Push failed — remote conflict or permission issue."""


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


# ---------------------------------------------------------------------------
# CachedWorkspaceManager — TTL-based repo clone cache
# ---------------------------------------------------------------------------


class CacheLockTimeoutError(Exception):
    """Could not acquire cache lock within the timeout window."""


class CachedWorkspaceManager:
    """TTL-based cache for git workspace clones with per-user keying.

    Caches clones in /tmp/bean_cache/{sha256(user_id:repo_url)[:16]}/ with
    sibling .meta timestamp files for TTL eviction and .lock files for
    concurrent-request safety.

    write operations (/chat):   acquire() + GitService.copy() to temp workspace
    read-only operations:       acquire() — use the returned cache path directly

    Token is never persisted — passed fresh on every acquire() call.
    """

    CACHE_ROOT = "/tmp/bean_cache"

    def __init__(self, ttl_seconds: int):
        self._ttl_seconds = ttl_seconds

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _cache_key(user_id: str, repo_url: str) -> str:
        raw = f"{user_id}:{repo_url}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _cache_path(self, key: str) -> str:
        return f"{self.CACHE_ROOT}/{key}"

    def _meta_path(self, key: str) -> str:
        return f"{self.CACHE_ROOT}/{key}.meta"

    def _is_valid(self, key: str) -> bool:
        if self._ttl_seconds == 0:
            return False
        if self._ttl_seconds == -1:
            return os.path.isdir(os.path.join(self._cache_path(key), ".git"))
        meta = Path(self._meta_path(key))
        if not meta.exists():
            return False
        cache_dir = self._cache_path(key)
        if not os.path.isdir(os.path.join(cache_dir, ".git")):
            return False
        age = time.time() - meta.stat().st_mtime
        return age < self._ttl_seconds

    def _touch(self, key: str) -> None:
        meta = Path(self._meta_path(key))
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(str(time.time()))

    @staticmethod
    def _refresh(cache_path: str, token: str | None) -> None:
        """Pull latest, falling back to fetch+reset on fast-forward failure."""
        askpass = None
        try:
            if token:
                askpass = GitService._configure_git_askpass(cache_path, token)
            rc, _, err = GitService._run_git(
                ["git", "pull", "--ff-only"], cwd=cache_path
            )
            if rc != 0:
                logger.warning(
                    "git pull --ff-only failed for %s, falling back to fetch+reset: %s",
                    cache_path, err,
                )
                GitService.fetch_reset(cache_path, token)
        finally:
            GitService._cleanup_askpass(askpass)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(
        self, user_id: str, repo_url: str, token: str | None = None
    ) -> str:
        """Get a fresh cache workspace path for (user_id, repo_url).

        Under a per-cache-key file lock:
        - If the entry is missing or TTL-expired: fresh clone.
        - Otherwise: git pull --ff-only (fallback: fetch+reset).
        - Updates the .meta access timestamp.

        Returns the cache directory path. The caller must NOT mutate this
        directory — for write operations, use GitService.copy() to create
        a per-request workspace first.
        """
        key = self._cache_key(user_id, repo_url)
        cache_path = self._cache_path(key)
        lock_path = f"{cache_path}.lock"

        os.makedirs(self.CACHE_ROOT, exist_ok=True)

        lock = FileLock(lock_path, timeout=30)
        try:
            with lock:
                if not self._is_valid(key):
                    logger.info(
                        "Cache miss or expired for key=%s, cloning %s", key, repo_url,
                    )
                    if os.path.exists(cache_path):
                        shutil.rmtree(cache_path, ignore_errors=True)
                    GitService.clone(cache_path, repo_url, token)
                else:
                    logger.info("Cache hit for key=%s, refreshing", key)
                    self._refresh(cache_path, token)
                self._touch(key)
        except TimeoutError:
            raise CacheLockTimeoutError(
                f"Timed out waiting for cache lock on key={key} (cache_path={cache_path})"
            )

        return cache_path

    def cleanup_expired(self) -> None:
        """Remove all expired cache entries from /tmp/bean_cache/.

        An entry is removed if its .meta timestamp is past TTL.
        Also removes orphaned cache dirs without a .meta file.
        TTL=0 removes everything. TTL=-1 removes nothing.
        """
        cache_root = Path(self.CACHE_ROOT)
        if not cache_root.is_dir():
            return

        if self._ttl_seconds == 0:
            logger.info("TTL=0, removing all cached workspaces")
            for entry in cache_root.iterdir():
                if entry.is_dir():
                    shutil.rmtree(str(entry), ignore_errors=True)
                elif entry.name.endswith(".meta") or entry.name.endswith(".lock"):
                    entry.unlink(missing_ok=True)
            return

        if self._ttl_seconds == -1:
            return

        now = time.time()
        for entry in sorted(cache_root.iterdir()):
            if not entry.is_dir():
                continue
            key = entry.name
            meta = Path(self._meta_path(key))
            if not meta.exists():
                logger.info("Removing orphaned cache dir %s (no .meta)", key)
                shutil.rmtree(str(entry), ignore_errors=True)
                continue
            if now - meta.stat().st_mtime >= self._ttl_seconds:
                logger.info(
                    "Removing expired cache dir %s (age=%.0fs)",
                    key, now - meta.stat().st_mtime,
                )
                shutil.rmtree(str(entry), ignore_errors=True)
                meta.unlink(missing_ok=True)
