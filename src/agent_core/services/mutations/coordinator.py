"""One validation and approved-application pipeline for mutation plans."""

import os
import subprocess
import tempfile
from dataclasses import dataclass

from ..beancount import Beancount, _cfg, _repo_path
from ..types import LedgerConfig, ValidationSummary
from . import sidecar
from .plans import FilePrecondition, MutationPlan
from .publisher import RepositoryPublisher


@dataclass(frozen=True)
class PlanValidation:
    touched_files: tuple[str, ...]
    validation: ValidationSummary
    check_output: str = ""


class MutationCoordinator:
    """Replays a plan identically in isolated and approved workspaces."""

    @staticmethod
    def _apply(workspace: str, plan: MutationPlan, config: LedgerConfig | None) -> tuple[str, ...]:
        touched: list[str] = []
        for operation in plan.operations:
            if operation.kind == "append":
                target = sidecar.append(workspace, operation.text, config)
            elif operation.kind == "open":
                target = sidecar.open_directive(
                    workspace, operation.account_name or "", operation.text, config
                )
            elif operation.kind == "replace":
                if not operation.target_file or operation.old_text is None:
                    raise ValueError("Replace mutation plan is missing its precondition")
                target = sidecar.replace(
                    workspace, operation.target_file, operation.old_text, operation.text, config
                )
            else:  # pragma: no cover - typed plans make this unreachable
                raise ValueError(f"Unsupported mutation operation: {operation.kind}")
            if target not in touched:
                touched.append(target)
        return tuple(touched)

    def validate(
        self, workspace: str, plan: MutationPlan, config: LedgerConfig | None = None
    ) -> PlanValidation:
        with tempfile.TemporaryDirectory(prefix="beanpilot-dry-run-") as tmp:
            dry_workspace = os.path.join(tmp, "workspace")
            sidecar.copy_workspace(workspace, dry_workspace)
            try:
                touched = self._apply(dry_workspace, plan, config)
                clean, output = Beancount.bean_check(dry_workspace, config)
                validation = (
                    ValidationSummary(status="validated", isolated=True)
                    if clean
                    else ValidationSummary(status="failed")
                )
                return PlanValidation(touched, validation, output)
            finally:
                Beancount.invalidate_workspace(dry_workspace)

    def apply_and_publish(
        self,
        workspace: str,
        plan: MutationPlan,
        repo_url: str,
        git_service: RepositoryPublisher,
        github_token: str | None = None,
        config: LedgerConfig | None = None,
    ) -> tuple[tuple[str, ...], dict, str]:
        # The sidecar directory is the only writable surface. Snapshot it before
        # applying so every validation, formatter, copy, and publishing failure
        # leaves the active workspace exactly as it started.
        resolved = _cfg(config)
        snapshot_paths = [resolved.sidecar_main_path, sidecar.sidecar_target_file(resolved)]
        snapshot_paths.extend(
            operation.target_file
            for operation in plan.operations
            if operation.target_file is not None
        )
        originals = sidecar.snapshot(workspace, list(dict.fromkeys(snapshot_paths)))
        try:
            if not self._preconditions_hold(workspace, plan):
                return (), {}, "MUTATION_PRECONDITION_FAILED"
            touched = self._apply(workspace, plan, config)
            clean, output = Beancount.bean_check(workspace, config)
            if not clean:
                sidecar.restore(workspace, originals)
                return touched, {}, output
            for rel_path in touched:
                Beancount.bean_format(workspace, _repo_path(workspace, rel_path))
            git = git_service.commit_and_push(
                workspace, plan.commit_message, repo_url, github_token
            )
            # A push rejection occurs after a successful local commit. Restoring
            # files here would leave HEAD and the worktree inconsistent; callers
            # receive the retryable publish error and the ephemeral workspace is
            # left matching its committed state.
            if not git.get("ok"):
                sidecar.restore(workspace, originals)
                # commit_and_push stages sidecar files before a local commit can
                # fail. Restore the index as well as file contents so a later
                # unrelated commit cannot publish a rejected mutation.
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
    def seal(
        workspace: str, plan: MutationPlan, config: LedgerConfig | None = None
    ) -> MutationPlan:
        resolved = _cfg(config)
        paths = [resolved.sidecar_main_path, sidecar.sidecar_target_file(resolved)]
        paths.extend(
            operation.target_file for operation in plan.operations if operation.target_file
        )
        originals = sidecar.snapshot(workspace, list(dict.fromkeys(paths)))
        return plan.with_preconditions(
            [FilePrecondition.from_content(path, content) for path, content in originals.items()]
        )

    @staticmethod
    def _preconditions_hold(workspace: str, plan: MutationPlan) -> bool:
        originals = sidecar.snapshot(
            workspace, [condition.path for condition in plan.preconditions]
        )
        return all(
            FilePrecondition.from_content(condition.path, originals[condition.path]).digest
            == condition.digest
            for condition in plan.preconditions
        )
