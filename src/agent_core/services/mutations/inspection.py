"""Read-only ledger inspection used by preflight and mutation planners."""

import os
import re
from pathlib import PurePosixPath

from ..beancount import Beancount, _cfg, _repo_path
from ..queries import LedgerQueryService
from ..types import LedgerConfig, PreflightResult
from .sidecar import sidecar_target_file


def include_line(entry_path: str, sidecar_main_path: str) -> str:
    relative = os.path.relpath(
        sidecar_main_path, start=PurePosixPath(entry_path).parent.as_posix()
    ).replace(os.sep, "/")
    return f'include "{relative}"'


def check_sidecar_include(workspace: str, ledger_config: LedgerConfig | None = None) -> bool:
    config = _cfg(ledger_config)
    try:
        with open(_repo_path(workspace, config.entry_path)) as handle:
            return include_line(config.entry_path, config.sidecar_main_path) in handle.read()
    except OSError:
        return False


def preflight_report(workspace: str, ledger_config: LedgerConfig | None = None) -> PreflightResult:
    """Return the historic preflight report without materializing sidecar files."""
    config = _cfg(ledger_config)
    if not check_sidecar_include(workspace, config):
        sidecar_include = include_line(config.entry_path, config.sidecar_main_path)[9:-1]
        return PreflightResult(
            status="SETUP_REQUIRED",
            action=f'Add include "{sidecar_include}" to {config.entry_path}',
        )
    target = sidecar_target_file(config)
    clean, output = Beancount.bean_check(workspace, config)
    recent = ""
    try:
        with open(_repo_path(workspace, target)) as handle:
            lines = handle.readlines()
        indices = [
            index for index, line in enumerate(lines) if re.match(r"^\d{4}-\d{2}-\d{2} ", line)
        ]
        start = indices[-5] if len(indices) >= 5 else (indices[0] if indices else 0)
        recent = "".join(lines[start:]).strip()
    except OSError:
        pass
    return PreflightResult(
        status="CLEAN" if clean else "ERROR",
        target=target,
        accounts=LedgerQueryService.get_accounts(workspace, config),
        errors=output.strip() if not clean else None,
        recent=recent,
    )
