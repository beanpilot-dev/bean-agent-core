"""Fail-closed policy for repository-provided Beancount plugins.

The Beancount loader can import Python modules referenced by ``plugin``
directives.  Private runtime requests must inspect the repository before any
loader/parser call so untrusted repository code is never executed implicitly.
"""

import re
from collections import deque
from pathlib import Path

from .types import LedgerConfig


class RepositoryPluginRejectedError(ValueError):
    """The repository asks Beancount to execute a plugin."""


_INCLUDE_RE = re.compile(r'^\s*include\s+"([^"]+)"\s*(?:;.*)?$')
_PLUGIN_RE = re.compile(r"^\s*plugin(?:\s|\t)")
_MAX_FILES = 256
_MAX_FILE_BYTES = 2_000_000
_MAX_TOTAL_BYTES = 16_000_000


def _relative_include(root: Path, including: Path, included: str) -> list[Path]:
    """Resolve one include without allowing path or symlink escapes."""
    requested = Path(included)
    if requested.is_absolute() or ".." in requested.parts or "\\" in included:
        raise RepositoryPluginRejectedError(
            "Repository include paths must stay inside the private workspace"
        )
    if any(part == ".git" for part in requested.parts):
        raise RepositoryPluginRejectedError(
            "Repository include paths must stay inside the ledger workspace"
        )
    pattern = including.parent / requested
    matches = (
        sorted(pattern.parent.glob(pattern.name))
        if any(char in included for char in "*?[")
        else [pattern]
    )
    safe: list[Path] = []
    for candidate in matches:
        if candidate.is_symlink() or any(parent.is_symlink() for parent in candidate.parents):
            raise RepositoryPluginRejectedError(
                "Repository include symlinks are disabled for private runtime requests"
            )
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise RepositoryPluginRejectedError(
                "Repository include paths must stay inside the private workspace"
            ) from exc
        if not candidate.exists() or not candidate.is_file():
            raise RepositoryPluginRejectedError(
                "Repository include could not be evaluated safely"
            )
        safe.append(candidate)
    return safe


def enforce_plugin_policy(workspace: str, config: LedgerConfig | None = None) -> None:
    """Reject executable Beancount plugins before parsing the ledger.

    This deliberately scans repository text files rather than using the
    Beancount parser to discover includes.  A plugin directive anywhere in a
    checked-in ledger is rejected because resolving the include graph itself
    may require loading untrusted content.  The error contains no repository
    content and is stable for the private runtime contract.
    """
    root = Path(workspace).resolve()
    ledger_config = config or LedgerConfig()
    try:
        entry = root / ledger_config.entry_path
        entry.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as exc:
        raise RepositoryPluginRejectedError(
            "Repository plugin policy could not be evaluated"
        ) from exc
    # Discovery/onboarding may intentionally open an empty repository before
    # an entry file exists. There is no parser input to execute in that case;
    # the normal setup/preflight path reports the missing ledger later.
    if not entry.exists():
        return
    pending = deque([entry])
    visited: set[Path] = set()
    total_bytes = 0
    while pending:
        if len(visited) >= _MAX_FILES:
            raise RepositoryPluginRejectedError(
                "Repository include graph exceeds the private runtime bound"
            )
        path = pending.popleft()
        if path in visited:
            continue
        visited.add(path)
        try:
            if path.is_symlink() or not path.is_file():
                raise RepositoryPluginRejectedError(
                    "Repository ledger files must be regular files"
                )
            size = path.stat().st_size
            if size > _MAX_FILE_BYTES or total_bytes + size > _MAX_TOTAL_BYTES:
                raise RepositoryPluginRejectedError(
                    "Repository ledger input exceeds the private runtime bound"
                )
            total_bytes += size
            text = path.read_text(encoding="utf-8", errors="strict")
            for line in text.splitlines():
                if _PLUGIN_RE.match(line):
                    raise RepositoryPluginRejectedError(
                        "Executable Beancount plugins are disabled for private runtime requests"
                    )
                include = _INCLUDE_RE.match(line)
                if include:
                    pending.extend(_relative_include(root, path, include.group(1)))
        except UnicodeDecodeError as exc:
            raise RepositoryPluginRejectedError(
                "Repository ledger text is not valid UTF-8"
            ) from exc
        except OSError as exc:
            raise RepositoryPluginRejectedError(
                "Repository plugin policy could not be evaluated"
            ) from exc
