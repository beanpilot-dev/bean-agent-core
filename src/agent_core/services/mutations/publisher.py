"""Narrow repository publishing ports used by approved mutation replay.

``RepositoryPublisher`` is the compatibility port used by the first mutation
plan rollout.  New approved execution must use ``ReconciledRepositoryPublisher``:
it makes the remote compare-and-swap and ambiguous-outcome reconciliation
boundary explicit.  A Git push and the SaaS pending-action transition are not
one atomic operation, so callers must reconcile an uncertain publication before
attempting another publish.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

_ACTION_MARKER = "Beanpilot-Pending-Action"
_PLAN_MARKER = "Beanpilot-Mutation-Plan"


def _marker_value(value: str, field: str) -> str:
    """Return a commit-trailer-safe identifier, rejecting header injection."""
    if not value or "\n" in value or "\r" in value:
        raise ValueError(f"{field} must be a non-empty single-line value")
    return value


@dataclass(frozen=True)
class MutationCommitMarker:
    """The immutable identity trailers carried by every canonical publish."""

    action_id: str
    plan_digest: str

    def __post_init__(self) -> None:
        _marker_value(self.action_id, "action_id")
        _marker_value(self.plan_digest, "plan_digest")

    def to_commit_trailers(self) -> str:
        """Serialize stable Git trailers for remote-history reconciliation."""
        return f"{_ACTION_MARKER}: {self.action_id}\n{_PLAN_MARKER}: {self.plan_digest}"

    @classmethod
    def from_commit_trailers(cls, message: str) -> "MutationCommitMarker | None":
        """Read the marker only when both trailers occur exactly once.

        Returning ``None`` distinguishes ordinary historical commits from a
        malformed marker.  A publisher that finds the action trailer without a
        valid matching plan digest must treat that as an integrity failure.
        """
        trailers: dict[str, list[str]] = {_ACTION_MARKER: [], _PLAN_MARKER: []}
        for line in message.splitlines():
            for name in trailers:
                prefix = f"{name}:"
                if line.startswith(prefix):
                    trailers[name].append(line.removeprefix(prefix).strip())
        if not trailers[_ACTION_MARKER] and not trailers[_PLAN_MARKER]:
            return None
        if len(trailers[_ACTION_MARKER]) != 1 or len(trailers[_PLAN_MARKER]) != 1:
            raise ValueError("Mutation commit marker is malformed")
        return cls(trailers[_ACTION_MARKER][0], trailers[_PLAN_MARKER][0])


@dataclass(frozen=True)
class PublishRequest:
    """Inputs for one compare-and-swap canonical mutation publication.

    ``expected_remote_head`` is the remote branch head observed immediately
    before publishing.  Implementations must use it as the lease/CAS value,
    rather than merely searching history for a marker before pushing.
    """

    marker: MutationCommitMarker
    base_commit: str
    expected_remote_head: str
    result_tree: str
    branch: str
    commit_message: str

    def __post_init__(self) -> None:
        for field in ("base_commit", "expected_remote_head", "result_tree", "branch"):
            _marker_value(getattr(self, field), field)


@dataclass(frozen=True)
class PublishReceipt:
    """Durable publication evidence returned by a successful canonical push."""

    action_id: str
    plan_digest: str
    base_commit: str
    published_commit: str
    result_tree: str
    branch: str

    def __post_init__(self) -> None:
        for field in (
            "action_id",
            "plan_digest",
            "base_commit",
            "published_commit",
            "result_tree",
            "branch",
        ):
            _marker_value(getattr(self, field), field)

    @property
    def marker(self) -> MutationCommitMarker:
        return MutationCommitMarker(self.action_id, self.plan_digest)


PublicationStatus = Literal["published", "not_found", "integrity_failure"]


@dataclass(frozen=True)
class PublicationReconciliation:
    """Result of refreshing remote history after an ambiguous publish result."""

    status: PublicationStatus
    receipt: PublishReceipt | None = None

    def __post_init__(self) -> None:
        if self.status == "published" and self.receipt is None:
            raise ValueError("A published reconciliation requires a publish receipt")
        if self.status != "published" and self.receipt is not None:
            raise ValueError("Only a published reconciliation may include a receipt")


def classify_reconciled_receipt(
    request: PublishRequest, receipt: PublishReceipt | None
) -> PublicationReconciliation:
    """Classify a receipt found after refreshing remote history.

    Remote-history traversal belongs to a concrete publisher.  This pure
    comparison keeps its safety rule consistent: a found action ID can only be
    treated as the prior publication when it carries the exact sealed-plan
    digest.  A different digest is an integrity failure, not a retry target.
    """
    if receipt is None or receipt.action_id != request.marker.action_id:
        return PublicationReconciliation("not_found")
    if receipt.plan_digest != request.marker.plan_digest:
        return PublicationReconciliation("integrity_failure")
    return PublicationReconciliation("published", receipt)


class RepositoryPublisher(Protocol):
    """Commit the already-validated sidecar changes to the repository."""

    def commit_and_push(
        self,
        workspace: str,
        message: str,
        repo_url: str,
        github_token: str | None = None,
        paths: Sequence[str] | None = None,
    ) -> dict: ...


class ReconciledRepositoryPublisher(Protocol):
    """CAS publisher for canonical plans, separate from the legacy port.

    ``reconcile_publication`` must refresh remote history and inspect marker
    identity before a retry.  Finding the same action ID with another plan
    digest is ``integrity_failure``, never a successful retry.
    """

    def publish_compare_and_swap(
        self,
        workspace: str,
        request: PublishRequest,
        repo_url: str,
        github_token: str | None = None,
    ) -> PublishReceipt: ...

    def reconcile_publication(
        self,
        request: PublishRequest,
        workspace: str,
        repo_url: str,
        github_token: str | None = None,
    ) -> PublicationReconciliation: ...
