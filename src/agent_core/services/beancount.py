"""Beancount infrastructure utilities: CLI helpers, parse cache, workspace fingerprinting.

This module is intentionally free of domain/LLM logic.  It owns:
  - _ParsedLedger   — lightweight dataclass produced by the parser cache
  - Beancount       — static CLI helpers (bean-check, bean-format, bean-query),
                      in-process parse cache, and workspace fingerprinting
  - _cfg / _repo_path — tiny helpers shared with LedgerService (imported there)

Kept separate from LedgerService so each class has a single clear responsibility
and can be tested in isolation.
"""

import io
import logging
import os
from dataclasses import dataclass
from pathlib import PurePosixPath

from .types import DEFAULT_LEDGER_CONFIG, LedgerConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared error type (also used by LedgerService)
# ---------------------------------------------------------------------------

class LedgerServiceError(Exception):
    """Unrecoverable ledger operation failure."""


# ---------------------------------------------------------------------------
# Tiny helpers (also imported by ledger.py for LedgerService use)
# ---------------------------------------------------------------------------

def _cfg(ledger_config: LedgerConfig | None) -> LedgerConfig:
    return ledger_config or DEFAULT_LEDGER_CONFIG


def _repo_path(workspace: str, rel_path: str) -> str:
    normalized = PurePosixPath(rel_path).as_posix().strip("/")
    parts = PurePosixPath(normalized).parts
    if not normalized or rel_path.startswith("/") or ".." in parts:
        raise LedgerServiceError("Ledger path must stay inside the repository")
    return os.path.join(workspace, *parts)


# ---------------------------------------------------------------------------
# Cache dataclass
# ---------------------------------------------------------------------------

@dataclass
class _ParsedLedger:
    fingerprint: tuple[tuple[str, int, int], ...]
    entries: list[object]
    errors: list[object]
    error_output: str


# ---------------------------------------------------------------------------
# Beancount CLI helpers
# ---------------------------------------------------------------------------

class Beancount:
    _parsed_cache: dict[tuple[str, str], _ParsedLedger] = {}

    @staticmethod
    def _bean_bin(workspace: str, name: str) -> str:
        import sys
        candidate = os.path.join(workspace, ".venv", "bin", name)
        if os.path.exists(candidate):
            return candidate
        candidate = os.path.join(os.path.dirname(sys.executable), name)
        if os.path.exists(candidate):
            return candidate
        return name

    @staticmethod
    def bean_check(
        workspace: str, ledger_config: LedgerConfig | None = None
    ) -> tuple[bool, str]:
        if os.environ.get("BEANPILOT_BEANCOUNT_VALIDATE_MODE") == "cli":
            return Beancount._bean_check_cli(workspace, ledger_config)

        parsed = Beancount._load_ledger(workspace, ledger_config)
        return not parsed.errors, parsed.error_output

    @staticmethod
    def _bean_check_cli(
        workspace: str, ledger_config: LedgerConfig | None = None
    ) -> tuple[bool, str]:
        import subprocess
        main = _repo_path(workspace, _cfg(ledger_config).entry_path)
        result = subprocess.run(
            [Beancount._bean_bin(workspace, "bean-check"), main],
            cwd=workspace, capture_output=True, text=True,
        )
        return result.returncode == 0, result.stdout + result.stderr

    @staticmethod
    def _cache_key(
        workspace: str, ledger_config: LedgerConfig | None = None
    ) -> tuple[str, str]:
        config = _cfg(ledger_config)
        return os.path.abspath(workspace), config.entry_path

    @staticmethod
    def _load_ledger(
        workspace: str, ledger_config: LedgerConfig | None = None
    ) -> _ParsedLedger:
        key = Beancount._cache_key(workspace, ledger_config)
        fingerprint = Beancount._workspace_fingerprint(workspace)
        cached = Beancount._parsed_cache.get(key)
        if cached is not None and cached.fingerprint == fingerprint:
            return cached

        from beancount import loader
        from beancount.parser import printer

        main = _repo_path(workspace, _cfg(ledger_config).entry_path)
        try:
            entries, errors, _options = loader.load_file(main)
            output = io.StringIO()
            printer.print_errors(errors, file=output)
            parsed = _ParsedLedger(
                fingerprint=fingerprint,
                entries=list(entries),
                errors=list(errors),
                error_output=output.getvalue(),
            )
        except Exception as exc:
            parsed = _ParsedLedger(
                fingerprint=fingerprint,
                entries=[],
                errors=[exc],
                error_output=str(exc),
            )

        Beancount._parsed_cache[key] = parsed
        return parsed

    @staticmethod
    def parsed_ledger(
        workspace: str, ledger_config: LedgerConfig | None = None
    ) -> _ParsedLedger:
        """Return the cached parsed ledger used by in-process validation."""
        return Beancount._load_ledger(workspace, ledger_config)

    @staticmethod
    def _workspace_fingerprint(workspace: str) -> tuple[tuple[str, int, int], ...]:
        fingerprint: list[tuple[str, int, int]] = []
        try:
            for dirpath, dirnames, filenames in os.walk(workspace):
                dirnames[:] = [d for d in dirnames if d not in {".git", ".venv"}]
                for filename in filenames:
                    if not filename.endswith((".beancount", ".py")):
                        continue
                    path = os.path.join(dirpath, filename)
                    try:
                        stat = os.stat(path)
                    except OSError:
                        continue
                    rel_path = os.path.relpath(path, workspace)
                    fingerprint.append((rel_path, stat.st_mtime_ns, stat.st_size))
        except OSError:
            pass
        return tuple(sorted(fingerprint))

    @staticmethod
    def invalidate_cache(
        workspace: str, ledger_config: LedgerConfig | None = None
    ) -> None:
        Beancount._parsed_cache.pop(Beancount._cache_key(workspace, ledger_config), None)

    @staticmethod
    def invalidate_workspace(workspace: str) -> None:
        workspace_abs = os.path.abspath(workspace)
        for key in list(Beancount._parsed_cache):
            if key[0] == workspace_abs:
                Beancount._parsed_cache.pop(key, None)

    @staticmethod
    def bean_format(workspace: str, file_path: str) -> None:
        import subprocess
        result = subprocess.run(
            [Beancount._bean_bin(workspace, "bean-format"), file_path],
            cwd=workspace, capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout:
            with open(file_path, "w") as f:
                f.write(result.stdout)
            Beancount.invalidate_workspace(workspace)
        elif result.returncode != 0:
            logger.warning(
                "bean-format failed on %s: %s", file_path, result.stderr.strip()
            )

    @staticmethod
    def run_bql_rows(
        workspace: str, bql: str, ledger_config: LedgerConfig | None = None
    ) -> tuple[list[dict], str | None]:
        import csv
        import io
        import subprocess
        main = _repo_path(workspace, _cfg(ledger_config).entry_path)
        result = subprocess.run(
            [Beancount._bean_bin(workspace, "bean-query"), "-f", "csv", main, bql],
            cwd=workspace, capture_output=True, text=True,
        )
        if result.returncode != 0:
            return [], result.stderr.strip()
        rows = list(csv.DictReader(io.StringIO(result.stdout)))
        return [{k: v.strip() for k, v in row.items()} for row in rows], None
