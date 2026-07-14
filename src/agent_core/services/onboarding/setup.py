"""Preview and confirmed setup mutations for ledger onboarding."""

import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Literal

from ..workspace import GitService
from .discovery import OnboardingDiscoveryService
from .paths import SafePathService

SetupOperation = Literal["initialize_ledger", "install_sidecar"]


class OnboardingSetupService:
    """Create or install the sidecar after an explicit setup confirmation."""

    DEFAULT_LEDGER_TITLE = "Personal Ledger"
    DEFAULT_OPERATING_CURRENCY = "USD"

    @classmethod
    def preview_setup(
        cls,
        workspace: str,
        *,
        operation: SetupOperation,
        entry_path: str | None = None,
        sidecar_main_path: str | None = None,
        sidecar_write_dir: str | None = None,
        ledger_title: str | None = None,
        operating_currency: str | None = None,
        current_head: Callable[[str], str] | None = None,
        repo_appears_clean: Callable[[str], bool] | None = None,
    ) -> dict[str, Any]:
        head_sha = (current_head or OnboardingDiscoveryService.current_head)(workspace)
        paths = SafePathService.setup_paths(
            workspace, operation, entry_path, sidecar_main_path, sidecar_write_dir
        )
        if "error" in paths:
            return {"status": "error", "code": paths["error"], "head_sha": head_sha, "changes": []}
        if operation == "initialize_ledger":
            entry_exists = Path(workspace, paths["entry_path"]).exists()
            sidecar_exists = Path(workspace, paths["sidecar_main_path"]).exists()
            clean = (repo_appears_clean or OnboardingDiscoveryService.repo_appears_clean)(workspace)
            if not clean:
                return {
                    "status": "error",
                    "code": "REPO_NOT_CLEAN",
                    "head_sha": head_sha,
                    "changes": [],
                }
            if entry_exists or sidecar_exists:
                return {
                    "status": "error",
                    "code": "TARGET_ALREADY_EXISTS",
                    "head_sha": head_sha,
                    "changes": [],
                }
        return {
            "status": "preview",
            "operation": operation,
            "head_sha": head_sha,
            "entry_path": paths["entry_path"],
            "sidecar_main_path": paths["sidecar_main_path"],
            "sidecar_write_dir": paths["sidecar_write_dir"],
            "include_line": paths["include_line"],
            "ledger_title": cls.starter_ledger_title(ledger_title)
            if operation == "initialize_ledger"
            else None,
            "operating_currency": cls.starter_operating_currency(operating_currency)
            if operation == "initialize_ledger"
            else None,
            "changes": cls.planned_changes(workspace, operation, paths),
            "events": [{"code": f"{operation}_previewed", "severity": "info"}],
        }

    preview = preview_setup

    @classmethod
    def confirm_setup(
        cls,
        workspace: str,
        *,
        operation: SetupOperation,
        expected_head_sha: str,
        repo_url: str,
        git_service: GitService,
        token: str | None,
        entry_path: str | None = None,
        sidecar_main_path: str | None = None,
        sidecar_write_dir: str | None = None,
        ledger_title: str | None = None,
        operating_currency: str | None = None,
        current_head: Callable[[str], str] | None = None,
        preview_setup: Callable[..., dict[str, Any]] | None = None,
        bean_check: Callable[[str, str], tuple[bool, str]] | None = None,
        bean_format: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        head = current_head or OnboardingDiscoveryService.current_head
        current = head(workspace)
        if expected_head_sha != current:
            return {
                "status": "stale",
                "code": "STALE_REPOSITORY",
                "head_sha": current,
                "expected_head_sha": expected_head_sha,
            }
        preview = (preview_setup or cls.preview_setup)(
            workspace,
            operation=operation,
            entry_path=entry_path,
            sidecar_main_path=sidecar_main_path,
            sidecar_write_dir=sidecar_write_dir,
            ledger_title=ledger_title,
            operating_currency=operating_currency,
        )
        if preview["status"] != "preview":
            return preview
        backup = Path(f"{workspace}.onboarding_backup")
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        shutil.copytree(workspace, backup, symlinks=True)
        try:
            paths = {
                key: preview.get(key)
                for key in (
                    "entry_path",
                    "sidecar_main_path",
                    "sidecar_write_dir",
                    "include_line",
                    "ledger_title",
                    "operating_currency",
                )
            }
            if operation == "initialize_ledger":
                cls.apply_initialize(workspace, paths)
            else:
                cls.apply_install_sidecar(workspace, paths)
            ok, _ = (bean_check or OnboardingDiscoveryService.bean_check)(
                workspace, paths["entry_path"]
            )
            if not ok:
                cls.restore_workspace(workspace, backup)
                return {
                    "status": "validation_failed",
                    "code": "BEANCOUNT_CHECK_FAILED",
                    "reverted": True,
                    "message": "Setup validation failed",
                }
            formatter = bean_format or cls.bean_format
            formatter(workspace, paths["entry_path"])
            if Path(workspace, paths["sidecar_main_path"]).exists():
                formatter(workspace, paths["sidecar_main_path"])
            commit = cls.commit_and_push_setup(
                workspace,
                f"chore(onboarding): {operation.replace('_', ' ')}",
                repo_url,
                git_service,
                token,
            )
            if not commit["ok"]:
                return {
                    "status": "dependency_unavailable",
                    "code": "GIT_COMMIT_FAILED",
                    "message": "Setup changes validated but could not be committed",
                }
            if isinstance(commit.get("push"), str) and commit["push"].startswith("PUSH_FAILED"):
                return {
                    "status": "dependency_unavailable",
                    "code": "GIT_PUSH_FAILED",
                    "message": "Setup commit could not be pushed",
                }
            return {
                "status": "success",
                "operation": operation,
                "head_sha": head(workspace),
                "entry_path": paths["entry_path"],
                "sidecar_main_path": paths["sidecar_main_path"],
                "sidecar_write_dir": paths["sidecar_write_dir"],
                "push_status": commit["push"],
                "events": [{"code": f"{operation}_confirmed", "severity": "info"}],
            }
        finally:
            shutil.rmtree(backup, ignore_errors=True)

    confirm = confirm_setup

    @staticmethod
    def planned_changes(
        workspace: str, operation: SetupOperation, paths: dict[str, str]
    ) -> list[dict[str, str]]:
        if operation == "initialize_ledger":
            return [
                {"action": "create", "path": paths["entry_path"]},
                {"action": "create", "path": paths["sidecar_main_path"]},
            ]
        changes = [{"action": "modify", "path": paths["entry_path"]}]
        if not Path(workspace, paths["sidecar_main_path"]).exists():
            changes.append({"action": "create", "path": paths["sidecar_main_path"]})
        return changes

    @classmethod
    def apply_initialize(cls, workspace: str, paths: dict[str, str | None]) -> None:
        entry = Path(workspace, str(paths["entry_path"]))
        sidecar = Path(workspace, str(paths["sidecar_main_path"]))
        entry.parent.mkdir(parents=True, exist_ok=True)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        title = cls.escape_beancount_string(paths.get("ledger_title"))
        currency = cls.starter_operating_currency(paths.get("operating_currency"))
        entry.write_text(
            f'option "title" "{title}"\n'
            f'option "operating_currency" "{currency}"\n\n'
            f"{paths['include_line']}\n",
            encoding="utf-8",
        )
        sidecar.write_text("; Agent sidecar - auto-managed\n", encoding="utf-8")

    @staticmethod
    def apply_install_sidecar(workspace: str, paths: dict[str, str | None]) -> None:
        entry = Path(workspace, str(paths["entry_path"]))
        sidecar = Path(workspace, str(paths["sidecar_main_path"]))
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        if not sidecar.exists():
            sidecar.write_text("; Agent sidecar - auto-managed\n", encoding="utf-8")
        content = entry.read_text(encoding="utf-8")
        if paths["include_line"] not in content:
            entry.write_text(content.rstrip() + f"\n\n{paths['include_line']}\n", encoding="utf-8")

    @staticmethod
    def restore_workspace(workspace: str, backup: Path) -> None:
        for child in Path(workspace).iterdir():
            if child.name == ".git":
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        for child in backup.iterdir():
            if child.name == ".git":
                continue
            target = Path(workspace, child.name)
            if child.is_dir():
                shutil.copytree(child, target, symlinks=True)
            else:
                shutil.copy2(child, target)

    @classmethod
    def starter_ledger_title(cls, value: str | None) -> str:
        title = (value or cls.DEFAULT_LEDGER_TITLE).strip()
        return title[:80] or cls.DEFAULT_LEDGER_TITLE

    @classmethod
    def starter_operating_currency(cls, value: str | None) -> str:
        currency = (value or cls.DEFAULT_OPERATING_CURRENCY).strip().upper()
        if (
            len(currency) < 2
            or len(currency) > 12
            or not currency[0].isalpha()
            or any(not char.isalnum() and char not in "._-" for char in currency)
        ):
            return cls.DEFAULT_OPERATING_CURRENCY
        return currency

    @classmethod
    def escape_beancount_string(cls, value: str | None) -> str:
        return cls.starter_ledger_title(value).replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def bean_format(workspace: str, entry_path: str) -> None:
        from ..ledger import Beancount

        entry = Path(workspace, entry_path)
        result = subprocess.run(
            [Beancount._bean_bin(workspace, "bean-format"), str(entry)],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout:
            entry.write_text(result.stdout, encoding="utf-8")

    @staticmethod
    def commit_and_push_setup(
        workspace: str, message: str, repo_url: str, git_service: GitService, token: str | None
    ) -> dict[str, Any]:
        subprocess.run(["git", "add", "-A"], cwd=workspace, capture_output=True, text=True)
        result = subprocess.run(
            ["git", "commit", "-m", message], cwd=workspace, capture_output=True, text=True
        )
        if result.returncode != 0:
            return {"ok": False, "error": "commit_failed", "push": None}
        try:
            push = git_service.push(workspace, repo_url, token)
        except Exception:
            return {"ok": True, "error": None, "push": "PUSH_FAILED"}
        return {"ok": True, "error": None, "push": push}
