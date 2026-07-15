"""Compatibility facade for the decomposed mutation replay components."""

from ..beancount import _cfg
from ..types import LedgerConfig
from .applier import MutationApplier
from .executor import MutationExecutor
from .persistence import SidecarMutationStore
from .plans import FilePrecondition, MutationPlan
from .publisher import RepositoryPublisher
from .sidecar import FilesystemSidecarMutationStore
from .targets import potential_write_targets
from .validator import MutationValidator, PlanValidation


class MutationCoordinator:
    """Compatibility facade; new code should depend on focused components."""

    def __init__(
        self,
        applier: MutationApplier | None = None,
        validator: MutationValidator | None = None,
        executor: MutationExecutor | None = None,
        store: SidecarMutationStore | None = None,
    ) -> None:
        self._store = store or FilesystemSidecarMutationStore()
        shared_applier = applier or MutationApplier(self._store)
        self._validator = validator or MutationValidator(
            shared_applier, store=self._store
        )
        self._executor = executor or MutationExecutor(
            shared_applier, store=self._store
        )

    def validate(
        self, workspace: str, plan: MutationPlan, config: LedgerConfig | None = None
    ) -> PlanValidation:
        return self._validator.validate(workspace, plan, config)

    def apply_and_publish(
        self,
        workspace: str,
        plan: MutationPlan,
        repo_url: str,
        git_service: RepositoryPublisher,
        github_token: str | None = None,
        config: LedgerConfig | None = None,
    ) -> tuple[tuple[str, ...], dict, str]:
        return self._executor.apply_and_publish(
            workspace, plan, repo_url, git_service, github_token, config
        )

    @staticmethod
    def seal(
        workspace: str,
        plan: MutationPlan,
        config: LedgerConfig | None = None,
        store: SidecarMutationStore | None = None,
    ) -> MutationPlan:
        resolved = _cfg(config)
        active_store = store or FilesystemSidecarMutationStore()
        originals = active_store.snapshot(
            workspace, potential_write_targets(plan, resolved), resolved
        )
        sealed = plan.with_preconditions(
            [
                FilePrecondition.from_content(path, content)
                for path, content in originals.files.items()
            ]
        )
        # New plans seal only the policy inputs declared by their handler.
        # The verifier still understands included_file_digest facts carried by
        # already-persisted plans, but adding that broad read set here would
        # make unrelated included-ledger edits stale every new approval.
        return sealed.with_semantic_facts(tuple(dict.fromkeys(plan.semantic_facts)))

    @staticmethod
    def _preconditions_hold(
        workspace: str,
        plan: MutationPlan,
        config: LedgerConfig | None = None,
    ) -> bool:
        """Compatibility hook for existing callers and tests."""
        return MutationExecutor.preconditions_hold(workspace, plan, config)
