"""Workspace management: git clone, pull, push for the beancount repo."""

import logging
import os
import subprocess

logger = logging.getLogger(__name__)


def _run_git(args: list[str], cwd: str, token: str | None = None) -> tuple[int, str, str]:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if token:
        env["GITHUB_TOKEN"] = token
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _authenticated_url(repo_url: str, token: str) -> str:
    """Embed token into https URL: https://x-token:<TOKEN>@github.com/..."""
    if repo_url.startswith("https://") and token:
        return repo_url.replace("https://", f"https://x-token:{token}@", 1)
    return repo_url


def _configure_git_identity(workspace: str) -> None:
    """Set a default git identity if none configured (needed inside containers)."""
    subprocess.run(
        ["git", "config", "user.email", "agent@local"],
        cwd=workspace,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Finance Agent"],
        cwd=workspace,
        capture_output=True,
    )


def ensure_workspace(workspace: str, repo_url: str | None = None, token: str | None = None) -> str:
    """Ensure the workspace is a valid git clone and up to date.

    - If workspace has no .git and repo_url is set: clone it.
    - If workspace already has .git: git pull.
    - If no repo_url and no .git: use as-is (local dev mode).

    Returns a status string.
    """
    os.makedirs(workspace, exist_ok=True)
    is_git_repo = os.path.isdir(os.path.join(workspace, ".git"))

    if not is_git_repo:
        if not repo_url:
            logger.info("No BEAN_REPO set — using local workspace as-is.")
            return "LOCAL"
        url = _authenticated_url(repo_url, token) if token else repo_url
        logger.info("Cloning repository into workspace")
        rc, _, err = _run_git(["git", "clone", url, "."], cwd=workspace, token=None)
        if rc != 0:
            raise RuntimeError(f"git clone failed: {err}")
        _configure_git_identity(workspace)
        logger.info("Clone complete.")
        return "CLONED"

    # Already a git repo — pull latest
    logger.info("Pulling latest from remote.")
    rc, out, err = _run_git(["git", "pull", "--ff-only"], cwd=workspace, token=token)
    if rc != 0:
        logger.warning("git pull failed (non-fatal)")
        return f"PULL_FAILED: {err}"
    status = out or "already up to date"
    logger.info("Pull complete")
    return f"PULLED: {status}"


def push(workspace: str, token: str | None = None) -> str:
    """Push committed changes to remote. Returns a status string."""
    rc, out, err = _run_git(["git", "push"], cwd=workspace, token=token)
    if rc != 0:
        logger.warning("git push failed")
        return f"PUSH_FAILED: {err}"
    status = out or "ok"
    logger.info("Push complete")
    return f"PUSHED: {status}"


def commit_and_push(
    workspace: str, message: str, token: str | None = None
) -> dict:
    """Stage data/, git commit, and push. Returns a result dict.

    Result keys:
        ok (bool)           — True if commit succeeded
        error (str|None)    — commit error message if ok=False
        push (str|None)     — push status string (PUSHED:... or PUSH_FAILED:...)
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
    push_status = push(workspace, token=token)
    if push_status.startswith("PUSH_FAILED"):
        logger.warning("git push failed — commit is local only")
    else:
        logger.info("git push ok")
    return {"ok": True, "error": None, "push": push_status}
