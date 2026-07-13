"""Approved mutation replay, final validation, formatting, and publication."""

import subprocess
from typing import Protocol

from ..beancount import Beancount, _cfg, _repo_path
from ..types import LedgerConfig
from . import sidecar
from .applier import MutationApplier
from .facts import semantic_facts_hold
from .plans import FilePrecondition, MutationPlan
from .publisher import RepositoryPublisher
from .validator import BeancountLedgerValidator, LedgerValidator


class LedgerFormatter(Protocol):
    """Narrow formatter dependency used only by approved execution."""

    def format(self, workspace: str, path: str) -> None: ...


class BeancountLedgerFormatter:
    """Production adapter for the narrow formatting port."""

    def format(self, workspace: str, path: str) -> None:
        Beancount.bean_format(workspace, path)


class MutationExecutor:
    """Execute a sealed plan after fresh precondition checks.

    This component owns the active-workspace transaction boundary.  Its
    publisher is supplied only at the final publishing step; the applier and
    validator paths never receive a repository publishing capability.
    """

    def __init__(
        self,
        applier: MutationApplier | None = None,
        ledger_validator: LedgerValidator | None = None,
        formatter: LedgerFormatter | None = None,
    ) -> None:
        self._applier = applier or MutationApplier()
        self._ledger_validator = ledger_validator or BeancountLedgerValidator()
        self._formatter = formatter or BeancountLedgerFormatter()

    def apply_and_publish(
        self,
        workspace: str,
        plan: MutationPlan,
        repo_url: str,
        git_service: RepositoryPublisher,
        github_token: str | None = None,
        config: LedgerConfig | None = None,
    ) -> tuple[tuple[str, ...], dict, str]:
        resolved = _cfg(config)
        snapshot_paths = [resolved.sidecar_main_path, sidecar.sidecar_target_file(resolved)]
        snapshot_paths.extend(
            operation.target_file
            for operation in plan.operations
            if operation.target_file is not None
        )
        originals = sidecar.snapshot(workspace, list(dict.fromkeys(snapshot_paths)))
        try:
            if not self.preconditions_hold(workspace, plan):
                return (), {}, "MUTATION_PRECONDITION_FAILED"
            touched = self._applier.apply(workspace, plan, config)
            clean, output = self._ledger_validator.check(workspace, config)
            if not clean:
                sidecar.restore(workspace, originals)
                return touched, {}, output
            for rel_path in touched:
                self._formatter.format(workspace, _repo_path(workspace, rel_path))
            git = git_service.commit_and_push(
                workspace, plan.commit_message, repo_url, github_token
            )
            if not git.get("ok"):
                sidecar.restore(workspace, originals)
                subprocess.run(
                    ["git", "reset", "--", *originals.keys()],
                    cwd=workspace,
                    capture_output=True,
                    check=False,
                )
            return touched, git, ""
        except Exception:
            sidecar.restore(workspace, originals)
            raise

    @staticmethod
    def preconditions_hold(workspace: str, plan: MutationPlan) -> bool:
        originals = sidecar.snapshot(
            workspace, [condition.path for condition in plan.preconditions]
        )
        files_hold = all(
            FilePrecondition.from_content(condition.path, originals[condition.path]).digest
            == condition.digest
            for condition in plan.preconditions
        )
        return files_hold and (
            not plan.semantic_facts or semantic_facts_hold(workspace, plan.semantic_facts)
        )
