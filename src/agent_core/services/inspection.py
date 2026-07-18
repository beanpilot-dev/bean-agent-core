"""Read-only ledger inspection and preflight reporting."""

import logging
import os
import shutil
import tempfile
import time
from datetime import date
from pathlib import PurePosixPath

from .beancount import Beancount, _cfg, _repo_path
from .ledger_paths import sidecar_target_file
from .preflight_context import all_accounts, build_ledger_context
from .types import LedgerConfig, PreflightResult

logger = logging.getLogger(__name__)


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
    timings: dict[str, float] = {}
    validation_started = time.perf_counter()
    if not check_sidecar_include(workspace, config):
        sidecar_include = include_line(config.entry_path, config.sidecar_main_path)[9:-1]
        return PreflightResult(
            status="SETUP_REQUIRED",
            action=f'Add include "{sidecar_include}" to {config.entry_path}',
            timings_ms={"validation": _elapsed_ms(validation_started)},
        )
    with tempfile.TemporaryDirectory(prefix="beanpilot-preflight-") as tmp:
        inspection_workspace = os.path.join(tmp, "workspace")
        shutil.copytree(workspace, inspection_workspace, ignore=shutil.ignore_patterns(".git"))
        try:
            target = _materialize_sidecar_for_inspection(inspection_workspace, config)
            clean, output = Beancount.bean_check(inspection_workspace, config)
            timings["validation"] = _elapsed_ms(validation_started)
            parsed = Beancount.parsed_ledger(inspection_workspace, config)
            try:
                context = build_ledger_context(
                    parsed.entries,
                    as_of=date.today(),
                    target=target,
                    raw_text=_read_target(inspection_workspace, target),
                    bean_check_passed=clean,
                    timings_ms=timings,
                )
            except Exception as error:
                logger.warning(
                    "preflight optional context failed section=ledger_context error_type=%s",
                    type(error).__name__,
                )
                return PreflightResult(
                    status="CLEAN" if clean else "ERROR",
                    target=target,
                    errors=output.strip() if not clean else None,
                    timings_ms=timings,
                )
            return PreflightResult(
                status="CLEAN" if clean else "ERROR",
                target=target,
                accounts=all_accounts(parsed.entries),
                accounts_by_type=context["accounts"],
                prompt_accounts=context["prompt_accounts"],
                accounts_scope=context["accounts_scope"],
                accounts_complete=context["accounts_complete"],
                prompt_accounts_omitted=context["prompt_accounts_omitted"],
                accounts_truncated=context["accounts_truncated"],
                accounts_omitted=context["accounts_omitted"],
                errors=output.strip() if not clean else None,
                recent=context["recent_ledger_text"].get("text", ""),
                ledger_meta=context["ledger_meta"],
                balance_snapshot=context["balance_snapshot"],
                flow_summary=context["flow_summary"],
                recent_activity=context["recent_activity"],
                recent_ledger_text=context["recent_ledger_text"],
                timings_ms=timings,
            )
        finally:
            Beancount.invalidate_workspace(inspection_workspace)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _read_target(workspace: str, target: str) -> str:
    try:
        with open(_repo_path(workspace, target), encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""
