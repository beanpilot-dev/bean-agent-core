"""PreflightService — deterministic ledger validation before LLM invocation.

Runs fail-fast checks: is the sidecar configured? Does the ledger parse
cleanly? What accounts exist? These run BEFORE the LangGraph graph is
invoked, saving LLM token costs when setup is incomplete or the repo
is unreachable.
"""

import logging
import os
import re

from .ledger import LedgerService, _check_sidecar_include
from .types import DEFAULT_LEDGER_CONFIG, LedgerConfig, PreflightResult

logger = logging.getLogger(__name__)


class PreflightError(Exception):
    """Unrecoverable preflight failure."""


class SetupRequiredError(PreflightError):
    """Sidecar include directive is missing from main.beancount."""


class BeancountSyntaxError(PreflightError):
    """bean-check failed — ledger contains syntax errors."""


class PreflightService:
    """Fail-fast deterministic ledger validation."""

    @staticmethod
    def validate(
        workspace: str, ledger_config: LedgerConfig | None = None
    ) -> PreflightResult:
        """Run full preflight: sidecar check + bean-check + account listing.

        Returns PreflightResult. Raises SetupRequiredError if sidecar
        include is missing — this is a hard block, not a soft warning.
        """
        config = ledger_config or DEFAULT_LEDGER_CONFIG
        if not _check_sidecar_include(workspace, config):
            msg = (
                f"Sidecar include directive is missing from {config.entry_path}."
            )
            raise SetupRequiredError(msg)

        return LedgerService.preflight_report(workspace, config)

    @staticmethod
    def check_setup(
        workspace: str, ledger_config: LedgerConfig | None = None
    ) -> bool:
        """Return True if the sidecar include directive is present."""
        return _check_sidecar_include(workspace, ledger_config)

    @staticmethod
    def list_accounts(
        workspace: str, ledger_config: LedgerConfig | None = None
    ) -> list[str]:
        """Return all account names from the ledger."""
        return LedgerService.get_accounts(workspace, ledger_config)

    @staticmethod
    def get_raw_open_directives(
        workspace: str, ledger_config: LedgerConfig | None = None
    ) -> list[str]:
        """Return all open directives from ledger .beancount files."""
        directives: list[str] = []
        data_dir = workspace
        try:
            for dirpath, dirnames, filenames in os.walk(data_dir):
                dirnames[:] = [d for d in dirnames if d not in {".git", ".venv"}]
                for fname in sorted(filenames):
                    if not fname.endswith(".beancount"):
                        continue
                    try:
                        with open(os.path.join(dirpath, fname)) as f:
                            for line in f:
                                if line.strip().startswith((";", "#", "*")):
                                    continue
                                if re.match(
                                    r"\d{4}-\d{2}-\d{2}\s+open\s+", line
                                ):
                                    directives.append(line.strip())
                    except OSError:
                        pass
        except OSError:
            pass
        return directives
