"""GitService — deterministic Git operations for ledger workspaces.

Handles clone, pull, fetch, push, commit, and cleanup. Token is
configured via GIT_ASKPASS to prevent credential leaks into .git/config.
"""

import hashlib
import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
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


class GitService(ABC):
    """Deterministic Git operations for ledger workspaces.

    Deployment mode is selected once by ``from_environment``. Git operations
    never inspect environment variables or infer mode from missing credentials.
    """

    @classmethod
    def from_environment(cls, mode: str, local_repo_url: str) -> "GitService":
        mode = mode.strip().lower()
        local_repo_url = local_repo_url.strip()
        if mode == "cloud":
            return CloudGitService()
        if mode == "local":
            if not local_repo_url:
                raise ValueError("LOCAL_REPO_URL is required when AGENT_MODE=local")
            repo_path = Path(local_repo_url).expanduser().resolve()
            if not repo_path.is_dir():
                raise ValueError(f"LOCAL_REPO_URL does not exist: {repo_path}")
            if not (
                (repo_path / ".git").is_dir()
                or (repo_path / "HEAD").is_file() and (repo_path / "objects").is_dir()
            ):
                raise ValueError(f"LOCAL_REPO_URL is not a Git repository: {repo_path}")
            return LocalGitService(str(repo_path))
        raise ValueError("AGENT_MODE must be either 'local' or 'cloud'")

    @abstractmethod
    def validate_request_credentials(self, repo_url: str, token: str | None) -> None:
        """Validate request credentials before orchestration starts."""

    @abstractmethod
    def _resolve_credentials(
        self, repo_url: str, token: str | None
    ) -> tuple[str, str | None]:
        """Return the repository URL and token used by Git operations."""

    def effective_repo_url(self, repo_url: str, token: str | None) -> str:
        """Return the repository URL selected by the constructed service."""
        effective_url, _ = self._resolve_credentials(repo_url, token)
        return effective_url

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _run_git(
        args: list[str], cwd: str, askpass: Path | None = None
    ) -> tuple[int, str, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if askpass:
            env["GIT_ASKPASS"] = str(askpass)
        result = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, env=env
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    @staticmethod
    def _configure_git_askpass(_workspace: str, token: str) -> Path:
        """Write a GIT_ASKPASS helper script so the token never touches URL or .git/config."""
        fd, askpass_name = tempfile.mkstemp(prefix="agent-git-askpass-")
        os.close(fd)
        askpass = Path(askpass_name)
        quoted_token = shlex.quote(token)
        askpass.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '  *Username*) echo "oauth2" ;;\n'
            f"  *) echo {quoted_token} ;;\n"
            "esac\n"
        )
        askpass.chmod(0o700)
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

    def clone(self, workspace: str, repo_url: str, token: str | None = None) -> str:
        """Clone repo_url into workspace.

        Returns a status string: "CLONED" or raises GitServiceError.
        """
        os.makedirs(workspace, exist_ok=True)
        effective_url, effective_token = self._resolve_credentials(repo_url, token)
        askpass = None

        try:
            if effective_token:
                askpass = self._configure_git_askpass(workspace, effective_token)

            logger.info("Cloning repository into workspace")
            rc, _, err = self._run_git(
                ["git", "clone", effective_url, "."], cwd=workspace, askpass=askpass
            )
            if rc != 0:
                raise self._classify_clone_error(err)

            self._configure_git_identity(workspace)
            logger.info("Clone complete")
            return "CLONED"
        finally:
            self._cleanup_askpass(askpass)

    def ensure_workspace(
        self, workspace: str, repo_url: str, token: str | None = None
    ) -> str:
        """Ensure workspace is a valid git repo and up to date.

        - If no .git: clone from repo_url.
        - If .git exists: git pull (fetch + merge, non-destructive).

        Returns a status string: CLONED, PULLED:..., or raises GitServiceError.
        """
        is_git_repo = os.path.isdir(os.path.join(workspace, ".git"))

        if not is_git_repo:
            return self.clone(workspace, repo_url, token)

        # Pull latest
        logger.info("Pulling latest")
        _, effective_token = self._resolve_credentials(repo_url, token)
        askpass = None
        try:
            if effective_token:
                askpass = self._configure_git_askpass(workspace, effective_token)
            rc, out, err = self._run_git(
                ["git", "pull", "--ff-only"], cwd=workspace, askpass=askpass
            )
            if rc != 0:
                logger.warning("git pull failed")
                return f"PULL_FAILED: {err}"
            return f"PULLED: {out or 'already up to date'}"
        finally:
            self._cleanup_askpass(askpass)

    def fetch_reset(
        self, workspace: str, repo_url: str, token: str | None = None
    ) -> str:
        """Fetch origin and reset hard to origin/HEAD. Fast cache refresh.

        Returns status string or raises GitServiceError.
        """
        _, effective_token = self._resolve_credentials(repo_url, token)
        askpass = None
        try:
            if effective_token:
                askpass = self._configure_git_askpass(workspace, effective_token)

            rc, _, err = self._run_git(
                ["git", "fetch", "origin"], cwd=workspace, askpass=askpass
            )
            if rc != 0:
                raise self._classify_clone_error(err)

            rc2, _, err2 = self._run_git(
                ["git", "reset", "--hard", "origin/HEAD"], cwd=workspace
            )
            if rc2 != 0:
                raise GitServiceError(f"git reset failed: {err2}")

            return "REFRESHED"
        finally:
            self._cleanup_askpass(askpass)

    def push(self, workspace: str, repo_url: str, token: str | None = None) -> str:
        """Push committed changes. Returns status string."""
        _, effective_token = self._resolve_credentials(repo_url, token)
        askpass = None
        try:
            if effective_token:
                askpass = self._configure_git_askpass(workspace, effective_token)
            rc, out, err = self._run_git(
                ["git", "push"], cwd=workspace, askpass=askpass
            )
            if rc != 0:
                raise PushRejectedError(f"Push failed: {err}")
            status = out or "ok"
            logger.info("Push complete")
            return f"PUSHED: {status}"
        finally:
            self._cleanup_askpass(askpass)

    def commit_and_push(
        self, workspace: str, message: str, repo_url: str, token: str | None = None
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
            logger.error("git commit failed")
            return {"ok": False, "error": result.stderr.strip(), "push": None}

        logger.info("git commit ok")
        try:
            push_status = self.push(workspace, repo_url, token)
        except PushRejectedError as e:
            logger.warning("git push failed")
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


class CloudGitService(GitService):
    """Git service for OAuth-authenticated cloud repositories."""

    def validate_request_credentials(self, repo_url: str, token: str | None) -> None:
        if not repo_url.strip():
            raise RepoAuthFailedError("Cloud repository URL is required")
        if not repo_url.startswith("https://"):
            raise RepoAuthFailedError("Cloud repository URL must use HTTPS")
        if not token or not token.strip():
            raise RepoAuthFailedError("Cloud repository OAuth token is required")

    def _resolve_credentials(
        self, repo_url: str, token: str | None
    ) -> tuple[str, str | None]:
        self.validate_request_credentials(repo_url, token)
        return repo_url, token


class LocalGitService(GitService):
    """Git service for a repository path selected at startup."""

    def __init__(self, local_repo_url: str):
        self._local_repo_url = local_repo_url

    def validate_request_credentials(self, repo_url: str, token: str | None) -> None:
        return None

    def _resolve_credentials(
        self, repo_url: str, token: str | None
    ) -> tuple[str, str | None]:
        return self._local_repo_url, None


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

    def __init__(self, git_service: GitService, ttl_seconds: int):
        self._git_service = git_service
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

    def _refresh(self, cache_path: str, repo_url: str, token: str | None) -> None:
        """Pull latest, falling back to fetch+reset on fast-forward failure."""
        _, effective_token = self._git_service._resolve_credentials(repo_url, token)
        askpass = None
        try:
            if effective_token:
                askpass = self._git_service._configure_git_askpass(
                    cache_path, effective_token
                )
            rc, _, err = self._git_service._run_git(
                ["git", "pull", "--ff-only"], cwd=cache_path, askpass=askpass
            )
            if rc != 0:
                logger.warning("git pull --ff-only failed, falling back to fetch+reset")
                self._git_service.fetch_reset(cache_path, repo_url, token)
        finally:
            self._git_service._cleanup_askpass(askpass)

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
        effective_repo_url = self._git_service.effective_repo_url(repo_url, token)
        key = self._cache_key(user_id, effective_repo_url)
        cache_path = self._cache_path(key)
        lock_path = f"{cache_path}.lock"

        os.makedirs(self.CACHE_ROOT, exist_ok=True)

        lock = FileLock(lock_path, timeout=30)
        try:
            with lock:
                if not self._is_valid(key):
                    logger.info("Cache miss or expired for key=%s", key)
                    if os.path.exists(cache_path):
                        shutil.rmtree(cache_path, ignore_errors=True)
                    self._git_service.clone(cache_path, repo_url, token)
                else:
                    logger.info("Cache hit for key=%s, refreshing", key)
                    self._refresh(cache_path, repo_url, token)
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
