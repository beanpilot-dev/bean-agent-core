"""Repository-relative path validation and sidecar path construction."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SetupOperation = Literal["initialize_ledger", "install_sidecar"]


@dataclass
class PathValidation:
    ok: bool
    path: str | None = None
    error_code: str | None = None


class SafePathService:
    """Validate all onboarding paths before repository access or mutation."""

    DEFAULT_ENTRY_PATH = "data/main.beancount"
    DEFAULT_SIDECAR_MAIN_PATH = "data/agent_inc/main.beancount"

    @staticmethod
    def validate_repo_path(
        workspace: str, path: str, *, must_exist: bool
    ) -> PathValidation:
        if not path or path.startswith("/"):
            return PathValidation(False, error_code="INVALID_ENTRY_PATH")
        root = Path(workspace).resolve()
        resolved = (root / path).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return PathValidation(False, error_code="PATH_TRAVERSAL")
        if must_exist and not resolved.is_file():
            return PathValidation(False, error_code="ENTRY_FILE_NOT_FOUND")
        return PathValidation(True, resolved.relative_to(root).as_posix())

    @staticmethod
    def include_line_for_entry(entry_path: str, sidecar_main_path: str) -> str:
        relative_path = os.path.relpath(Path(sidecar_main_path), start=Path(entry_path).parent)
        return f'include "{Path(relative_path).as_posix()}"'

    @classmethod
    def setup_paths(
        cls,
        workspace: str,
        operation: SetupOperation,
        entry_path: str | None,
        sidecar_main_path: str | None,
        sidecar_write_dir: str | None,
    ) -> dict[str, str]:
        entry = entry_path or cls.DEFAULT_ENTRY_PATH
        entry_validation = cls.validate_repo_path(
            workspace, entry, must_exist=operation == "install_sidecar"
        )
        if not entry_validation.ok or not entry_validation.path:
            return {"error": entry_validation.error_code or "INVALID_ENTRY_PATH"}
        entry = entry_validation.path
        sidecar_main = sidecar_main_path or (
            cls.DEFAULT_SIDECAR_MAIN_PATH
            if operation == "initialize_ledger"
            else str(Path(entry).parent / "agent_inc" / "main.beancount")
        )
        sidecar_dir = sidecar_write_dir or str(Path(sidecar_main).parent)
        main_validation = cls.validate_repo_path(workspace, sidecar_main, must_exist=False)
        directory_validation = cls.validate_repo_path(workspace, sidecar_dir, must_exist=False)
        if not main_validation.ok or not main_validation.path:
            return {"error": main_validation.error_code or "INVALID_SIDECAR_PATH"}
        if not directory_validation.ok or not directory_validation.path:
            return {"error": directory_validation.error_code or "INVALID_SIDECAR_DIR"}
        sidecar_main = main_validation.path
        sidecar_dir = directory_validation.path
        if entry == sidecar_main:
            return {"error": "SETUP_PATH_ALIAS"}
        if Path(sidecar_main).parent.as_posix() != sidecar_dir:
            return {"error": "SIDECAR_PATH_MISMATCH"}
        return {
            "entry_path": entry,
            "sidecar_main_path": sidecar_main,
            "sidecar_write_dir": sidecar_dir,
            "include_line": cls.include_line_for_entry(entry, sidecar_main),
        }
