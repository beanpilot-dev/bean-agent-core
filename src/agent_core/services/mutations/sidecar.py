"""Sidecar-only workspace helpers used by the mutation coordinator."""

import os
import re
import shutil
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Sequence

from ..beancount import Beancount, _cfg, _repo_path
from ..ledger_paths import is_sidecar_path, sidecar_target_file
from ..types import LedgerConfig
from .persistence import SidecarSnapshot


def _require_sidecar_path(
    workspace: str, rel_path: str, ledger_config: LedgerConfig | None
) -> str:
    """Return a sidecar path only when no symlink can redirect the write."""
    config = _cfg(ledger_config)
    normalized = PurePosixPath(rel_path).as_posix().strip("/")
    if not is_sidecar_path(normalized, config):
        raise ValueError("Sidecar mutation path must stay inside sidecar_write_dir")
    workspace_root = Path(workspace).resolve()
    write_root = workspace_root.joinpath(*PurePosixPath(config.sidecar_write_dir).parts)
    candidate = workspace_root.joinpath(*PurePosixPath(normalized).parts)
    for path in (write_root, candidate):
        current = workspace_root
        for part in path.relative_to(workspace_root).parts:
            current /= part
            if current.is_symlink():
                raise ValueError("Sidecar mutation paths must not contain symlinks")
    resolved_write_root = write_root.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if (
        not resolved_write_root.is_relative_to(workspace_root)
        or not resolved_candidate.is_relative_to(resolved_write_root)
    ):
        raise ValueError("Sidecar mutation path must stay inside the workspace")
    return normalized


def ensure_sidecar(workspace: str, ledger_config: LedgerConfig | None = None) -> str:
    config = _cfg(ledger_config)
    target = sidecar_target_file(config)
    target_path = _repo_path(workspace, target)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    if not os.path.exists(target_path):
        with open(target_path, "w") as handle:
            handle.write(f"; Agent-generated transactions — {date.today():%Y-%m}\n")
    main_path = _repo_path(workspace, config.sidecar_main_path)
    os.makedirs(os.path.dirname(main_path), exist_ok=True)
    include_line = f'include "{date.today():%Y-%m}.beancount"\n'
    existing = ""
    try:
        with open(main_path) as handle:
            existing = handle.read()
    except FileNotFoundError:
        pass
    if f"{date.today():%Y-%m}.beancount" not in existing:
        with open(main_path, "w") as handle:
            if existing:
                handle.write(existing)
                if not existing.endswith("\n"):
                    handle.write("\n")
            else:
                handle.write("; Agent sidecar — auto-managed, do not edit manually\n")
            handle.write(include_line)
    Beancount.invalidate_cache(workspace, config)
    return target


def append(workspace: str, text: str, ledger_config: LedgerConfig | None = None) -> str:
    target = ensure_sidecar(workspace, ledger_config)
    with open(_repo_path(workspace, target), "a") as handle:
        handle.write(f"\n{text.strip()}\n")
    Beancount.invalidate_cache(workspace, ledger_config)
    return target


def open_directive(
    workspace: str,
    account_name: str,
    directive_text: str,
    ledger_config: LedgerConfig | None = None,
) -> str:
    config = _cfg(ledger_config)
    ensure_sidecar(workspace, config)
    main_path = _repo_path(workspace, config.sidecar_main_path)
    with open(main_path) as handle:
        original = handle.read()
    lines = original.splitlines()
    account_type = account_name.split(":")[0]
    insert_after = -1
    for index, line in enumerate(lines):
        if re.match(rf"^\d{{4}}-\d{{2}}-\d{{2}} open {account_type}", line):
            insert_after = index
    if insert_after < 0:
        for index, line in enumerate(lines):
            if re.match(r"^\d{4}-\d{2}-\d{2} open ", line):
                insert_after = index
    directive_lines = directive_text.splitlines()
    if insert_after >= 0:
        lines[insert_after + 1 : insert_after + 1] = directive_lines
        content = "\n".join(lines) + "\n"
    else:
        content = original.rstrip("\n") + "\n\n" + directive_text + "\n"
    with open(main_path, "w") as handle:
        handle.write(content)
    Beancount.invalidate_cache(workspace, config)
    return config.sidecar_main_path


def replace(
    workspace: str,
    rel_path: str,
    old_text: str,
    new_text: str,
    ledger_config: LedgerConfig | None = None,
) -> str:
    rel_path = _require_sidecar_path(workspace, rel_path, ledger_config)
    path = _repo_path(workspace, rel_path)
    with open(path) as handle:
        original = handle.read()
    if old_text not in original:
        raise ValueError("Mutation precondition failed: target transaction changed")
    with open(path, "w") as handle:
        handle.write(original.replace(old_text, new_text.strip(), 1))
    Beancount.invalidate_cache(workspace, ledger_config)
    return rel_path


def copy_workspace(workspace: str, target: str) -> None:
    shutil.copytree(
        workspace,
        target,
        symlinks=True,
        ignore=shutil.ignore_patterns(
            ".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"
        ),
    )


def snapshot(workspace: str, rel_paths: list[str]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for rel_path in rel_paths:
        path = _repo_path(workspace, rel_path)
        try:
            with open(path, encoding="utf-8") as handle:
                result[rel_path] = handle.read()
        except FileNotFoundError:
            result[rel_path] = None
    return result


def restore(workspace: str, originals: dict[str, str | None]) -> None:
    created_parents: set[str] = set()
    for rel_path, content in originals.items():
        path = _repo_path(workspace, rel_path)
        if content is None:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            created_parents.add(os.path.dirname(path))
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
    workspace_abs = os.path.abspath(workspace)
    for parent in sorted(created_parents, key=len, reverse=True):
        while parent.startswith(workspace_abs) and parent != workspace_abs:
            try:
                os.rmdir(parent)
            except OSError:
                break
            parent = os.path.dirname(parent)
    Beancount.invalidate_workspace(workspace)


class FilesystemSidecarMutationStore:
    """Filesystem adapter for the sidecar mutation persistence port.

    The module-level helpers remain as compatibility hooks for existing callers
    and tests.  Canonical callers depend on this adapter through the protocol.
    """

    def append(
        self,
        workspace: str,
        text: str,
        config: LedgerConfig | None = None,
    ) -> str:
        config = _cfg(config)
        _require_sidecar_path(workspace, config.sidecar_main_path, config)
        _require_sidecar_path(workspace, sidecar_target_file(config), config)
        return append(workspace, text, config)

    def open_directive(
        self,
        workspace: str,
        account_name: str,
        directive_text: str,
        config: LedgerConfig | None = None,
    ) -> str:
        config = _cfg(config)
        _require_sidecar_path(workspace, config.sidecar_main_path, config)
        _require_sidecar_path(workspace, sidecar_target_file(config), config)
        return open_directive(workspace, account_name, directive_text, config)

    def replace(
        self,
        workspace: str,
        rel_path: str,
        old_text: str,
        new_text: str,
        config: LedgerConfig | None = None,
    ) -> str:
        return replace(workspace, rel_path, old_text, new_text, config)

    def copy_workspace(self, workspace: str, target: str) -> None:
        copy_workspace(workspace, target)

    def snapshot(
        self,
        workspace: str,
        rel_paths: Sequence[str],
        config: LedgerConfig | None = None,
    ) -> SidecarSnapshot:
        paths = [_require_sidecar_path(workspace, path, config) for path in rel_paths]
        return SidecarSnapshot(snapshot(workspace, paths))

    def restore(
        self,
        workspace: str,
        captured: SidecarSnapshot,
        config: LedgerConfig | None = None,
    ) -> None:
        paths = {
            _require_sidecar_path(workspace, path, config): content
            for path, content in captured.files.items()
        }
        restore(workspace, paths)
