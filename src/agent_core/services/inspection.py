"""Read-only ledger inspection and preflight reporting."""

import os
import re
import shutil
import tempfile
from pathlib import PurePosixPath

from .beancount import Beancount, _cfg, _repo_path
from .ledger_paths import sidecar_target_file
from .queries import LedgerQueryService
from .types import LedgerConfig, PreflightResult


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


def _materialize_sidecar_for_inspection(workspace: str, config: LedgerConfig) -> str:
    """Emulate historic sidecar creation only inside an inspection copy."""
    target = sidecar_target_file(config)
    target_path = _repo_path(workspace, target)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    if not os.path.exists(target_path):
        with open(target_path, "w") as handle:
            handle.write(f"; Agent-generated transactions — {target[-17:-7]}\n")
    main_path = _repo_path(workspace, config.sidecar_main_path)
    os.makedirs(os.path.dirname(main_path), exist_ok=True)
    include = f'include "{os.path.basename(target)}"\n'
    try:
        with open(main_path) as handle:
            existing = handle.read()
    except FileNotFoundError:
        existing = ""
    if os.path.basename(target) not in existing:
        with open(main_path, "w") as handle:
            handle.write(existing or "; Agent sidecar — auto-managed, do not edit manually\n")
            if existing and not existing.endswith("\n"):
                handle.write("\n")
            handle.write(include)
    return target


def preflight_report(workspace: str, ledger_config: LedgerConfig | None = None) -> PreflightResult:
    """Return the historic report without changing the active workspace."""
    config = _cfg(ledger_config)
    if not check_sidecar_include(workspace, config):
        sidecar_include = include_line(config.entry_path, config.sidecar_main_path)[9:-1]
        return PreflightResult(
            status="SETUP_REQUIRED",
            action=f'Add include "{sidecar_include}" to {config.entry_path}',
        )
    with tempfile.TemporaryDirectory(prefix="beanpilot-preflight-") as tmp:
        inspection_workspace = os.path.join(tmp, "workspace")
        shutil.copytree(workspace, inspection_workspace, ignore=shutil.ignore_patterns(".git"))
        try:
            target = _materialize_sidecar_for_inspection(inspection_workspace, config)
            clean, output = Beancount.bean_check(inspection_workspace, config)
            recent = ""
            with open(_repo_path(inspection_workspace, target)) as handle:
                lines = handle.readlines()
            indices = [
                index for index, line in enumerate(lines) if re.match(r"^\d{4}-\d{2}-\d{2} ", line)
            ]
            start = indices[-5] if len(indices) >= 5 else (indices[0] if indices else 0)
            recent = "".join(lines[start:]).strip()
            return PreflightResult(
                status="CLEAN" if clean else "ERROR",
                target=target,
                accounts=LedgerQueryService.get_accounts(inspection_workspace, config),
                errors=output.strip() if not clean else None,
                recent=recent,
            )
        finally:
            Beancount.invalidate_workspace(inspection_workspace)
