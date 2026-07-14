"""Compatibility façade for deterministic ledger onboarding services."""

from typing import Any

from .discovery import DiscoveryStatus, OnboardingDiscoveryService
from .paths import PathValidation, SafePathService
from .setup import OnboardingSetupService, SetupOperation


class OnboardingService:
    """Preserve the original onboarding API while delegating by responsibility."""

    DEFAULT_ENTRY_PATH = OnboardingDiscoveryService.DEFAULT_ENTRY_PATH
    DEFAULT_SIDECAR_MAIN_PATH = SafePathService.DEFAULT_SIDECAR_MAIN_PATH
    DEFAULT_SIDECAR_WRITE_DIR = "data/agent_inc"
    DEFAULT_INCLUDE_LINE = 'include "agent_inc/main.beancount"'
    DEFAULT_LEDGER_TITLE = OnboardingSetupService.DEFAULT_LEDGER_TITLE
    DEFAULT_OPERATING_CURRENCY = OnboardingSetupService.DEFAULT_OPERATING_CURRENCY
    MAX_DISCOVERY_VALIDATIONS = OnboardingDiscoveryService.MAX_DISCOVERY_VALIDATIONS
    ROOT_NAME_HINTS = OnboardingDiscoveryService.ROOT_NAME_HINTS

    @staticmethod
    def discover(workspace: str, **kwargs: Any) -> dict[str, Any]:
        return OnboardingDiscoveryService.discover(
            workspace,
            bean_check=OnboardingService.bean_check,
            account_count=OnboardingService.account_count,
            **kwargs,
        )

    @staticmethod
    def preview_setup(workspace: str, **kwargs: Any) -> dict[str, Any]:
        return OnboardingSetupService.preview_setup(
            workspace,
            current_head=OnboardingService.current_head,
            repo_appears_clean=OnboardingService._repo_appears_clean,
            **kwargs,
        )

    @staticmethod
    def confirm_setup(workspace: str, **kwargs: Any) -> dict[str, Any]:
        return OnboardingSetupService.confirm_setup(
            workspace,
            current_head=OnboardingService.current_head,
            preview_setup=OnboardingService.preview_setup,
            bean_check=OnboardingService.bean_check,
            bean_format=OnboardingService.bean_format,
            **kwargs,
        )

    current_head = staticmethod(OnboardingDiscoveryService.current_head)
    sidecar_status = staticmethod(OnboardingDiscoveryService.sidecar_status)
    bean_check = staticmethod(OnboardingDiscoveryService.bean_check)
    account_count = staticmethod(OnboardingDiscoveryService.account_count)
    bean_format = staticmethod(OnboardingSetupService.bean_format)

    _beancount_files = staticmethod(OnboardingDiscoveryService._beancount_files)
    _discover_candidates = staticmethod(OnboardingDiscoveryService._discover_candidates)
    _candidate_paths = staticmethod(OnboardingDiscoveryService._candidate_paths)
    _rg_candidate_files = staticmethod(OnboardingDiscoveryService._rg_candidate_files)
    _python_candidate_files = staticmethod(OnboardingDiscoveryService._python_candidate_files)
    _repo_appears_clean = staticmethod(OnboardingDiscoveryService.repo_appears_clean)
    _cheap_candidate_for_path = staticmethod(OnboardingDiscoveryService._cheap_candidate_for_path)
    _validated_candidate_for_path = staticmethod(
        OnboardingDiscoveryService._validated_candidate_for_path
    )
    _validate_repo_path = staticmethod(SafePathService.validate_repo_path)
    _include_line_for_entry = staticmethod(SafePathService.include_line_for_entry)
    _setup_paths = staticmethod(SafePathService.setup_paths)
    _planned_changes = staticmethod(OnboardingSetupService.planned_changes)
    _apply_initialize = staticmethod(OnboardingSetupService.apply_initialize)
    _apply_install_sidecar = staticmethod(OnboardingSetupService.apply_install_sidecar)
    _restore_workspace = staticmethod(OnboardingSetupService.restore_workspace)
    _starter_ledger_title = staticmethod(OnboardingSetupService.starter_ledger_title)
    _starter_operating_currency = staticmethod(OnboardingSetupService.starter_operating_currency)
    _escape_beancount_string = staticmethod(OnboardingSetupService.escape_beancount_string)
    _commit_and_push_setup = staticmethod(OnboardingSetupService.commit_and_push_setup)


__all__ = ["DiscoveryStatus", "OnboardingService", "PathValidation", "SetupOperation"]
