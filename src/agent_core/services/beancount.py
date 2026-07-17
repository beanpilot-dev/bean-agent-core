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
from datetime import date
from pathlib import PurePosixPath
from typing import Any

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
class ParsedLedgerAccount:
    """Lifecycle facts for one native account from the included ledger."""

    account_name: str
    status: str
    open_date: date | None = None
    close_date: date | None = None
    declared_currencies: tuple[str, ...] = ()
    display_name: str | None = None


@dataclass
class _ParsedLedger:
    fingerprint: tuple[tuple[str, int, int], ...]
    entries: list[object]
    errors: list[object]
    error_output: str
    account_index: tuple[ParsedLedgerAccount, ...] = ()


_NATIVE_ACCOUNT_ROOTS = {"Assets", "Liabilities", "Equity", "Income", "Expenses"}


def _native_account(value: object) -> str | None:
    if not isinstance(value, str) or value.split(":", 1)[0] not in _NATIVE_ACCOUNT_ROOTS:
        return None
    return value if ":" in value else None


def _safe_display_name(meta: object) -> str | None:
    if not isinstance(meta, dict):
        return None
    value = meta.get("name")
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized[:200] or None


def _build_account_index(entries: list[object]) -> tuple[ParsedLedgerAccount, ...]:
    """Build account lifecycle facts from the parsed include graph only."""
    facts: dict[str, dict[str, Any]] = {}

    def ensure(account: object) -> dict[str, Any] | None:
        account_name = _native_account(account)
        if account_name is None:
            return None
        return facts.setdefault(
            account_name,
            {
                "open_dates": [],
                "close_dates": [],
                "currencies": set(),
                "display_name": None,
                "last_lifecycle": None,
            },
        )

    for entry in entries:
        entry_type = entry.__class__.__name__
        account_fact = ensure(getattr(entry, "account", None))
        if account_fact is not None:
            entry_date = getattr(entry, "date", None)
            if entry_type == "Open":
                if isinstance(entry_date, date):
                    account_fact["open_dates"].append(entry_date)
                    current = account_fact["last_lifecycle"]
                    if current is None or entry_date >= current[0]:
                        account_fact["last_lifecycle"] = (entry_date, "open")
                currencies = getattr(entry, "currencies", None) or ()
                account_fact["currencies"].update(
                    currency for currency in currencies if isinstance(currency, str)
                )
                display_name = _safe_display_name(getattr(entry, "meta", None))
                if display_name is not None:
                    account_fact["display_name"] = display_name
            elif entry_type == "Close":
                if isinstance(entry_date, date):
                    account_fact["close_dates"].append(entry_date)
                    current = account_fact["last_lifecycle"]
                    if current is None or entry_date >= current[0]:
                        account_fact["last_lifecycle"] = (entry_date, "closed")

        for posting in getattr(entry, "postings", ()) or ():
            ensure(getattr(posting, "account", None))

    indexed: list[ParsedLedgerAccount] = []
    for account_name in sorted(facts):
        fact = facts[account_name]
        last_lifecycle = fact["last_lifecycle"]
        status = last_lifecycle[1] if last_lifecycle is not None else "open"
        indexed.append(
            ParsedLedgerAccount(
                account_name=account_name,
                status=status,
                open_date=min(fact["open_dates"], default=None),
                close_date=max(fact["close_dates"], default=None),
                declared_currencies=tuple(sorted(fact["currencies"])),
                display_name=fact["display_name"],
            )
        )
    return tuple(indexed)


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
            parsed.account_index = _build_account_index(parsed.entries)
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
