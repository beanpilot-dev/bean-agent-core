"""Narrow repository publishing port used by approved mutation replay."""

from typing import Protocol


class RepositoryPublisher(Protocol):
    """Commit the already-validated sidecar changes to the repository."""

    def commit_and_push(
        self, workspace: str, message: str, repo_url: str, github_token: str | None = None
    ) -> dict: ...
