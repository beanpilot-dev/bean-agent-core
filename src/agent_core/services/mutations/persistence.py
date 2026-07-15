"""Persistence ports for deterministic sidecar mutation replay."""

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence

from ..types import LedgerConfig


@dataclass(frozen=True)
class SidecarSnapshot:
    """Original sidecar file contents captured for rollback and sealing."""

    files: Mapping[str, str | None]


class SidecarMutationStore(Protocol):
    """Narrow filesystem capability shared by validation and execution.

    Implementations may copy a workspace for isolated validation and may mutate
    or restore only files within the configured sidecar write directory.  Git
    staging, committing, and publishing deliberately do not belong here.
    """

    def append(
        self,
        workspace: str,
        text: str,
        config: LedgerConfig | None = None,
    ) -> str: ...

    def open_directive(
        self,
        workspace: str,
        account_name: str,
        directive_text: str,
        config: LedgerConfig | None = None,
    ) -> str: ...

    def replace(
        self,
        workspace: str,
        rel_path: str,
        old_text: str,
        new_text: str,
        config: LedgerConfig | None = None,
    ) -> str: ...

    def copy_workspace(self, workspace: str, target: str) -> None: ...

    def snapshot(
        self,
        workspace: str,
        rel_paths: Sequence[str],
        config: LedgerConfig | None = None,
    ) -> SidecarSnapshot: ...

    def restore(
        self,
        workspace: str,
        snapshot: SidecarSnapshot,
        config: LedgerConfig | None = None,
    ) -> None: ...
