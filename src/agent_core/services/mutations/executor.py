"""Approved mutation replay, final validation, formatting, and publication."""

import subprocess
from typing import Protocol

from ..beancount import Beancount, _cfg, _repo_path
from ..types import LedgerConfig
from .applier import MutationApplier
from .facts import semantic_facts_hold
from .persistence import SidecarMutationStore
from .plans import FilePrecondition, MutationPlan
from .publisher import RepositoryPublisher
from .sidecar import FilesystemSidecarMutationStore
from .targets import potential_write_targets, sealed_write_set_matches
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
        store: SidecarMutationStore | None = None,
    ) -> None:
        self._store = store or FilesystemSidecarMutationStore()
        self._applier = applier or MutationApplier(self._store)
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
        # Direct coordinator callers may execute an unsealed plan in tests or
        # internal workflows. Once preconditions exist, however, replay must
        # never derive a different target set from the one that was sealed.
        if plan.preconditions and not sealed_write_set_matches(plan, resolved):
            return (), {}, "MUTATION_PLAN_WRITE_SET_MISMATCH"
        write_targets = potential_write_targets(plan, resolved)
        originals = self._store.snapshot(
            workspace, write_targets, resolved
        )
        try:
            if not self.preconditions_hold(workspace, plan, resolved, self._store):
                return (), {}, "MUTATION_PRECONDITION_FAILED"
            touched = self._applier.apply(workspace, plan, config)
            clean, output = self._ledger_validator.check(workspace, config)
            if not clean:
                self._store.restore(workspace, originals, resolved)
                return touched, {}, output
            applied = self._store.snapshot(workspace, tuple(originals.files), resolved)
            changed_paths = tuple(
                path
                for path, original in originals.files.items()
                if applied.files[path] != original
            )
            if not changed_paths:
                return touched, {
                    "ok": False,
                    "error": "No validated ledger changes to publish",
                    "push": None,
                }, ""
            for rel_path in changed_paths:
                self._formatter.format(workspace, _repo_path(workspace, rel_path))
            git = git_service.commit_and_push(
                workspace, plan.commit_message, repo_url, github_token, changed_paths
            )
            if not git.get("ok"):
                self._store.restore(workspace, originals, resolved)
                self._reset_index(workspace, tuple(originals.files))
            return touched, git, ""
        except Exception:
            self._store.restore(workspace, originals, resolved)
            self._reset_index(workspace, tuple(originals.files))
            raise

    @staticmethod
    def _reset_index(workspace: str, paths: tuple[str, ...]) -> None:
        subprocess.run(
            ["git", "reset", "--", *paths],
            cwd=workspace,
            capture_output=True,
            check=False,
        )

    @staticmethod
    def preconditions_hold(
        workspace: str,
        plan: MutationPlan,
        config: LedgerConfig | None = None,
        store: SidecarMutationStore | None = None,
    ) -> bool:
        active_store = store or FilesystemSidecarMutationStore()
        originals = active_store.snapshot(
            workspace,
            [condition.path for condition in plan.preconditions],
            config,
        )
        files_hold = all(
            FilePrecondition.from_content(condition.path, originals.files[condition.path]).digest
            == condition.digest
            for condition in plan.preconditions
        )
        return files_hold and (
            not plan.semantic_facts or semantic_facts_hold(workspace, plan.semantic_facts, config)
        )
