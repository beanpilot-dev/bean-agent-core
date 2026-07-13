"""Compatibility facade for the decomposed mutation replay components."""

from ..beancount import _cfg
from ..types import LedgerConfig
from . import sidecar
from .applier import MutationApplier
from .executor import MutationExecutor
from .facts import capture_ledger_read_facts
from .plans import FilePrecondition, MutationPlan
from .publisher import RepositoryPublisher
from .validator import MutationValidator, PlanValidation


class MutationCoordinator:
    """Compatibility facade; new code should depend on focused components."""

    def __init__(
        self,
        applier: MutationApplier | None = None,
        validator: MutationValidator | None = None,
        executor: MutationExecutor | None = None,
    ) -> None:
        shared_applier = applier or MutationApplier()
        self._validator = validator or MutationValidator(shared_applier)
        self._executor = executor or MutationExecutor(shared_applier)

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
        workspace: str, plan: MutationPlan, config: LedgerConfig | None = None
    ) -> MutationPlan:
        resolved = _cfg(config)
        paths = [resolved.sidecar_main_path, sidecar.sidecar_target_file(resolved)]
        paths.extend(
            operation.target_file for operation in plan.operations if operation.target_file
        )
        originals = sidecar.snapshot(workspace, list(dict.fromkeys(paths)))
        sealed = plan.with_preconditions(
            [FilePrecondition.from_content(path, content) for path, content in originals.items()]
        )
        # Handler-declared facts record action-specific policy inputs; the
        # common include graph protects the broader ledger read surface.
        facts = (*capture_ledger_read_facts(workspace, config), *plan.semantic_facts)
        unique_facts = tuple(dict.fromkeys(facts))
        return sealed.with_semantic_facts(unique_facts)

    @staticmethod
    def _preconditions_hold(workspace: str, plan: MutationPlan) -> bool:
        """Compatibility hook for existing callers and tests."""
        return MutationExecutor.preconditions_hold(workspace, plan)
