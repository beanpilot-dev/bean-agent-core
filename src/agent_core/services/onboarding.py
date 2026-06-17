"""Deterministic repository discovery and setup for ledger onboarding."""

import csv
import io
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .ledger import Beancount
from .workspace import GitService

DiscoveryStatus = Literal[
    "clean_repo",
    "one_candidate",
    "multiple_candidates",
    "no_candidate",
    "repo_unreachable",
    "repo_auth_failed",
    "invalid_request",
]

SetupOperation = Literal["initialize_ledger", "install_sidecar"]


@dataclass
class PathValidation:
    ok: bool
    path: str | None = None
    error_code: str | None = None


class OnboardingService:
    """Repository inspection and setup mutations for onboarding.

    This service never uses the LLM and never persists repository contents.
    Callers own workspace lifecycle and token handling.
    """

    DEFAULT_ENTRY_PATH = "data/main.beancount"
    DEFAULT_SIDECAR_MAIN_PATH = "data/agent_inc/main.beancount"
    DEFAULT_SIDECAR_WRITE_DIR = "data/agent_inc"
    DEFAULT_INCLUDE_LINE = 'include "agent_inc/main.beancount"'
    MAX_DISCOVERY_VALIDATIONS = 8
    ROOT_NAME_HINTS = {"main.beancount", "root.beancount", "ledger.beancount"}

    @staticmethod
    def discover(
        workspace: str,
        *,
        entry_path: str | None = None,
        expected_head_sha: str | None = None,
    ) -> dict[str, Any]:
        head_sha = OnboardingService.current_head(workspace)
        events = [{"code": "repo_scanned", "severity": "info"}]

        if expected_head_sha and expected_head_sha != head_sha:
            return {
                "status": "ok",
                "discovery_status": "invalid_request",
                "head_sha": head_sha,
                "stale": True,
                "error": {"code": "STALE_REPOSITORY"},
                "candidates": [],
                "events": events + [{"code": "stale_head", "severity": "warn"}],
            }

        if entry_path:
            validation = OnboardingService._validate_repo_path(
                workspace, entry_path, must_exist=True
            )
            if not validation.ok or not validation.path:
                return {
                    "status": "ok",
                    "discovery_status": "invalid_request",
                    "head_sha": head_sha,
                    "stale": False,
                    "error": {"code": validation.error_code or "INVALID_ENTRY_PATH"},
                    "candidates": [],
                    "events": events + [{"code": "entry_path_invalid", "severity": "warn"}],
                }
            candidate = OnboardingService._validated_candidate_for_path(
                workspace, validation.path
            )
            selected = candidate if candidate["validation"]["status"] == "valid" else None
            return {
                "status": "ok",
                "discovery_status": "one_candidate" if selected else "invalid_request",
                "head_sha": head_sha,
                "stale": False,
                "selected_entry_path": validation.path if selected else None,
                "candidates": [candidate],
                "sidecar": (
                    OnboardingService.sidecar_status(workspace, validation.path)
                    if selected
                    else None
                ),
                "events": events + [{"code": "entry_path_validated", "severity": "info"}],
                "error": None if selected else {"code": "INVALID_BEANCOUNT_ENTRY"},
            }

        candidates = OnboardingService._discover_candidates(workspace)
        valid_candidates = [
            candidate
            for candidate in candidates
            if candidate["validation"]["status"] == "valid"
        ]
        valid_candidates.sort(
            key=lambda item: (item["confidence"], item["path"] == "data/main.beancount"),
            reverse=True,
        )

        if len(valid_candidates) == 1 or (
            len(valid_candidates) > 1
            and valid_candidates[0]["confidence"] > valid_candidates[1]["confidence"]
        ):
            selected = valid_candidates[0]
            discovery_status: DiscoveryStatus = "one_candidate"
        elif len(valid_candidates) > 1:
            selected = None
            discovery_status = "multiple_candidates"
        elif OnboardingService._repo_appears_clean(workspace):
            selected = None
            discovery_status = "clean_repo"
        else:
            selected = None
            discovery_status = "no_candidate"

        return {
            "status": "ok",
            "discovery_status": discovery_status,
            "head_sha": head_sha,
            "stale": False,
            "selected_entry_path": selected["path"] if selected else None,
            "candidates": candidates,
            "sidecar": (
                OnboardingService.sidecar_status(workspace, selected["path"])
                if selected
                else None
            ),
            "events": events
            + [{"code": discovery_status, "severity": "info" if valid_candidates else "warn"}],
            "error": None,
        }

    @staticmethod
    def preview_setup(
        workspace: str,
        *,
        operation: SetupOperation,
        entry_path: str | None = None,
        sidecar_main_path: str | None = None,
        sidecar_write_dir: str | None = None,
    ) -> dict[str, Any]:
        head_sha = OnboardingService.current_head(workspace)
        paths = OnboardingService._setup_paths(
            workspace, operation, entry_path, sidecar_main_path, sidecar_write_dir
        )
        if "error" in paths:
            return {
                "status": "error",
                "code": paths["error"],
                "head_sha": head_sha,
                "changes": [],
            }
        if operation == "initialize_ledger":
            entry_exists = Path(workspace, paths["entry_path"]).exists()
            sidecar_exists = Path(workspace, paths["sidecar_main_path"]).exists()
            if not OnboardingService._repo_appears_clean(workspace):
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

        changes = OnboardingService._planned_changes(workspace, operation, paths)
        return {
            "status": "preview",
            "operation": operation,
            "head_sha": head_sha,
            "entry_path": paths["entry_path"],
            "sidecar_main_path": paths["sidecar_main_path"],
            "sidecar_write_dir": paths["sidecar_write_dir"],
            "include_line": paths["include_line"],
            "changes": changes,
            "events": [{"code": f"{operation}_previewed", "severity": "info"}],
        }

    @staticmethod
    def confirm_setup(
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
    ) -> dict[str, Any]:
        current_head = OnboardingService.current_head(workspace)
        if expected_head_sha != current_head:
            return {
                "status": "stale",
                "code": "STALE_REPOSITORY",
                "head_sha": current_head,
                "expected_head_sha": expected_head_sha,
            }

        preview = OnboardingService.preview_setup(
            workspace,
            operation=operation,
            entry_path=entry_path,
            sidecar_main_path=sidecar_main_path,
            sidecar_write_dir=sidecar_write_dir,
        )
        if preview["status"] != "preview":
            return preview

        backup = Path(f"{workspace}.onboarding_backup")
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        shutil.copytree(workspace, backup, symlinks=True)
        try:
            paths = {
                "entry_path": preview["entry_path"],
                "sidecar_main_path": preview["sidecar_main_path"],
                "sidecar_write_dir": preview["sidecar_write_dir"],
                "include_line": preview["include_line"],
            }
            if operation == "initialize_ledger":
                OnboardingService._apply_initialize(workspace, paths)
            else:
                OnboardingService._apply_install_sidecar(workspace, paths)

            ok, output = OnboardingService.bean_check(workspace, paths["entry_path"])
            if not ok:
                OnboardingService._restore_workspace(workspace, backup)
                return {
                    "status": "validation_failed",
                    "code": "BEANCOUNT_CHECK_FAILED",
                    "reverted": True,
                    "message": "Setup validation failed",
                }

            OnboardingService.bean_format(workspace, paths["entry_path"])
            if Path(workspace, paths["sidecar_main_path"]).exists():
                OnboardingService.bean_format(workspace, paths["sidecar_main_path"])

            commit = OnboardingService._commit_and_push_setup(
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

            new_head = OnboardingService.current_head(workspace)
            return {
                "status": "success",
                "operation": operation,
                "head_sha": new_head,
                "entry_path": paths["entry_path"],
                "sidecar_main_path": paths["sidecar_main_path"],
                "sidecar_write_dir": paths["sidecar_write_dir"],
                "push_status": commit["push"],
                "events": [{"code": f"{operation}_confirmed", "severity": "info"}],
            }
        finally:
            shutil.rmtree(backup, ignore_errors=True)

    @staticmethod
    def current_head(workspace: str) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    @staticmethod
    def sidecar_status(workspace: str, entry_path: str) -> dict[str, Any]:
        sidecar_write_dir = str(Path(entry_path).parent / "agent_inc")
        sidecar_main_path = str(Path(sidecar_write_dir) / "main.beancount")
        include_line = OnboardingService._include_line_for_entry(entry_path, sidecar_main_path)
        entry_abs = Path(workspace, entry_path)
        configured = False
        try:
            configured = include_line in entry_abs.read_text(encoding="utf-8")
        except OSError:
            configured = False
        return {
            "status": "configured" if configured else "missing",
            "include_line": include_line,
            "sidecar_main_path": sidecar_main_path,
            "sidecar_write_dir": sidecar_write_dir,
        }

    @staticmethod
    def bean_check(workspace: str, entry_path: str) -> tuple[bool, str]:
        entry = Path(workspace, entry_path)
        result = subprocess.run(
            [Beancount._bean_bin(workspace, "bean-check"), str(entry)],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0, result.stdout + result.stderr

    @staticmethod
    def bean_format(workspace: str, entry_path: str) -> None:
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
    def _beancount_files(workspace: str) -> list[str]:
        root = Path(workspace)
        paths: list[str] = []
        for path in root.rglob("*.beancount"):
            if ".git" in path.parts:
                continue
            paths.append(path.relative_to(root).as_posix())
        return sorted(paths)

    @staticmethod
    def _discover_candidates(workspace: str) -> list[dict[str, Any]]:
        """Cheap-score likely root files, then validate only the top candidates."""
        paths = OnboardingService._candidate_paths(workspace)
        cheap_candidates = [
            OnboardingService._cheap_candidate_for_path(workspace, path)
            for path in paths
        ]
        cheap_candidates.sort(
            key=lambda item: (
                item["confidence"],
                item["path"] == OnboardingService.DEFAULT_ENTRY_PATH,
            ),
            reverse=True,
        )

        validated: list[dict[str, Any]] = []
        for index, candidate in enumerate(cheap_candidates):
            if index < OnboardingService.MAX_DISCOVERY_VALIDATIONS:
                validated.append(
                    OnboardingService._validated_candidate_for_path(
                        workspace, candidate["path"], base_candidate=candidate
                    )
                )
            else:
                skipped = dict(candidate)
                skipped["validation"] = {"status": "not_checked", "account_count": 0}
                validated.append(skipped)
        return validated

    @staticmethod
    def _candidate_paths(workspace: str) -> list[str]:
        all_files = OnboardingService._beancount_files(workspace)
        hinted = {
            path
            for path in all_files
            if Path(path).name.lower() in OnboardingService.ROOT_NAME_HINTS
        }
        content_matches = set(OnboardingService._rg_candidate_files(workspace))
        return sorted(hinted | content_matches)

    @staticmethod
    def _rg_candidate_files(workspace: str) -> list[str]:
        pattern = r'option\s+"|^\s*include\s+"|^\d{4}-\d{2}-\d{2}\s+open\s+'
        try:
            result = subprocess.run(
                [
                    "rg",
                    "-l",
                    "--glob",
                    "*.beancount",
                    pattern,
                    ".",
                ],
                cwd=workspace,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return OnboardingService._python_candidate_files(workspace)
        if result.returncode not in {0, 1}:
            return OnboardingService._python_candidate_files(workspace)
        return sorted(
            line.removeprefix("./")
            for line in result.stdout.splitlines()
            if line.strip()
        )

    @staticmethod
    def _python_candidate_files(workspace: str) -> list[str]:
        matches: list[str] = []
        for path in OnboardingService._beancount_files(workspace):
            try:
                content = Path(workspace, path).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = Path(workspace, path).read_text(errors="ignore")
            except OSError:
                continue
            if (
                'option "' in content
                or "\ninclude " in f"\n{content}"
                or " open " in content
            ):
                matches.append(path)
        return sorted(matches)

    @staticmethod
    def _repo_appears_clean(workspace: str) -> bool:
        for child in Path(workspace).iterdir():
            if child.name == ".git":
                continue
            if child.is_dir() and not any(child.rglob("*")):
                continue
            return False
        return True

    @staticmethod
    def _validate_repo_path(
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
    def _cheap_candidate_for_path(workspace: str, path: str) -> dict[str, Any]:
        file_path = Path(workspace, path)
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = file_path.read_text(errors="ignore")
        except OSError:
            content = ""

        reasons: list[str] = []
        confidence = 0
        if file_path.name.lower() in OnboardingService.ROOT_NAME_HINTS:
            confidence += 35
            reasons.append("root filename hint")
        if 'option "' in content:
            confidence += 25
            reasons.append("contains options")
        if "\ninclude " in f"\n{content}":
            confidence += 20
            reasons.append("includes other files")
        if " open " in content:
            confidence += 15
            reasons.append("contains open directives")
        if "agent_inc" in Path(path).parts:
            confidence -= 25
            reasons.append("inside agent sidecar")
        return {
            "path": path,
            "confidence": max(0, min(confidence, 100)),
            "reason": "; ".join(reasons) if reasons else "beancount extension",
            "validation": {"status": "not_checked", "account_count": 0},
        }

    @staticmethod
    def _validated_candidate_for_path(
        workspace: str, path: str, base_candidate: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        candidate = base_candidate or OnboardingService._cheap_candidate_for_path(
            workspace, path
        )
        ok, _output = OnboardingService.bean_check(workspace, path)
        account_count = 0
        accounts_listed = False
        if ok:
            accounts_listed, account_count = OnboardingService.account_count(
                workspace, path
            )
        confidence = int(candidate["confidence"])
        reasons = [str(candidate["reason"])] if candidate["reason"] else []
        if ok:
            confidence += 25
            reasons.append("bean-check passed")
        return {
            "path": path,
            "confidence": max(0, min(confidence, 100)),
            "reason": "; ".join(reason for reason in reasons if reason),
            "validation": {
                "status": "valid" if ok and accounts_listed else "invalid",
                "account_count": account_count,
            },
        }

    @staticmethod
    def account_count(workspace: str, entry_path: str) -> tuple[bool, int]:
        entry = Path(workspace, entry_path)
        result = subprocess.run(
            [
                Beancount._bean_bin(workspace, "bean-query"),
                "-f",
                "csv",
                str(entry),
                "SELECT DISTINCT account ORDER BY account",
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False, 0
        rows = list(csv.DictReader(io.StringIO(result.stdout)))
        return True, len(rows)

    @staticmethod
    def _include_line_for_entry(entry_path: str, sidecar_main_path: str) -> str:
        rel = os.path.relpath(
            Path(sidecar_main_path),
            start=Path(entry_path).parent,
        )
        return f'include "{Path(rel).as_posix()}"'

    @staticmethod
    def _setup_paths(
        workspace: str,
        operation: SetupOperation,
        entry_path: str | None,
        sidecar_main_path: str | None,
        sidecar_write_dir: str | None,
    ) -> dict[str, str]:
        entry = entry_path or OnboardingService.DEFAULT_ENTRY_PATH
        entry_validation = OnboardingService._validate_repo_path(
            workspace, entry, must_exist=operation == "install_sidecar"
        )
        if not entry_validation.ok or not entry_validation.path:
            return {"error": entry_validation.error_code or "INVALID_ENTRY_PATH"}
        entry = entry_validation.path

        sidecar_main = sidecar_main_path or (
            OnboardingService.DEFAULT_SIDECAR_MAIN_PATH
            if operation == "initialize_ledger"
            else str(Path(entry).parent / "agent_inc" / "main.beancount")
        )
        sidecar_dir = sidecar_write_dir or str(Path(sidecar_main).parent)
        sidecar_main_validation = OnboardingService._validate_repo_path(
            workspace, sidecar_main, must_exist=False
        )
        sidecar_dir_validation = OnboardingService._validate_repo_path(
            workspace, sidecar_dir, must_exist=False
        )
        if not sidecar_main_validation.ok or not sidecar_main_validation.path:
            return {"error": sidecar_main_validation.error_code or "INVALID_SIDECAR_PATH"}
        if not sidecar_dir_validation.ok or not sidecar_dir_validation.path:
            return {"error": sidecar_dir_validation.error_code or "INVALID_SIDECAR_DIR"}
        sidecar_main = sidecar_main_validation.path
        sidecar_dir = sidecar_dir_validation.path
        if entry == sidecar_main:
            return {"error": "SETUP_PATH_ALIAS"}
        if Path(sidecar_main).parent.as_posix() != sidecar_dir:
            return {"error": "SIDECAR_PATH_MISMATCH"}

        include_line = OnboardingService._include_line_for_entry(entry, sidecar_main)
        return {
            "entry_path": entry,
            "sidecar_main_path": sidecar_main,
            "sidecar_write_dir": sidecar_dir,
            "include_line": include_line,
        }

    @staticmethod
    def _planned_changes(
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

    @staticmethod
    def _apply_initialize(workspace: str, paths: dict[str, str]) -> None:
        entry = Path(workspace, paths["entry_path"])
        sidecar = Path(workspace, paths["sidecar_main_path"])
        entry.parent.mkdir(parents=True, exist_ok=True)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text(
            'option "title" "Personal Ledger"\n'
            'option "operating_currency" "USD"\n\n'
            f'{paths["include_line"]}\n',
            encoding="utf-8",
        )
        sidecar.write_text(
            "; Agent sidecar - auto-managed\n",
            encoding="utf-8",
        )

    @staticmethod
    def _apply_install_sidecar(workspace: str, paths: dict[str, str]) -> None:
        entry = Path(workspace, paths["entry_path"])
        sidecar = Path(workspace, paths["sidecar_main_path"])
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        if not sidecar.exists():
            sidecar.write_text("; Agent sidecar - auto-managed\n", encoding="utf-8")
        content = entry.read_text(encoding="utf-8")
        if paths["include_line"] not in content:
            entry.write_text(content.rstrip() + f'\n\n{paths["include_line"]}\n', encoding="utf-8")

    @staticmethod
    def _restore_workspace(workspace: str, backup: Path) -> None:
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

    @staticmethod
    def _commit_and_push_setup(
        workspace: str,
        message: str,
        repo_url: str,
        git_service: GitService,
        token: str | None,
    ) -> dict[str, Any]:
        subprocess.run(["git", "add", "-A"], cwd=workspace, capture_output=True, text=True)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return {"ok": False, "error": "commit_failed", "push": None}
        try:
            push = git_service.push(workspace, repo_url, token)
        except Exception:
            return {"ok": True, "error": None, "push": "PUSH_FAILED"}
        return {"ok": True, "error": None, "push": push}
