"""Read-only ledger-entry discovery for onboarding."""

import csv
import io
import subprocess
from pathlib import Path
from typing import Any, Callable, Literal

from ..ledger import Beancount
from .paths import SafePathService

DiscoveryStatus = Literal[
    "clean_repo",
    "one_candidate",
    "multiple_candidates",
    "no_candidate",
    "repo_unreachable",
    "repo_auth_failed",
    "invalid_request",
]


class OnboardingDiscoveryService:
    """Inspect a repository without changing its ledger files."""

    DEFAULT_ENTRY_PATH = "data/main.beancount"
    MAX_DISCOVERY_VALIDATIONS = 8
    ROOT_NAME_HINTS = {"main.beancount", "root.beancount", "ledger.beancount"}

    @classmethod
    def discover(
        cls,
        workspace: str,
        *,
        entry_path: str | None = None,
        expected_head_sha: str | None = None,
        bean_check: Callable[[str, str], tuple[bool, str]] | None = None,
        account_count: Callable[[str, str], tuple[bool, int]] | None = None,
    ) -> dict[str, Any]:
        check = bean_check or cls.bean_check
        count_accounts = account_count or cls.account_count
        head_sha = cls.current_head(workspace)
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
            validation = SafePathService.validate_repo_path(workspace, entry_path, must_exist=True)
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
            candidate = cls._validated_candidate_for_path(
                workspace, validation.path, bean_check=check, account_count=count_accounts
            )
            selected = candidate if candidate["validation"]["status"] == "valid" else None
            return {
                "status": "ok",
                "discovery_status": "one_candidate" if selected else "invalid_request",
                "head_sha": head_sha,
                "stale": False,
                "selected_entry_path": validation.path if selected else None,
                "candidates": [candidate],
                "sidecar": cls.sidecar_status(workspace, validation.path) if selected else None,
                "events": events + [{"code": "entry_path_validated", "severity": "info"}],
                "error": None if selected else {"code": "INVALID_BEANCOUNT_ENTRY"},
            }

        candidates = cls._discover_candidates(
            workspace, bean_check=check, account_count=count_accounts
        )
        valid_candidates = [item for item in candidates if item["validation"]["status"] == "valid"]
        valid_candidates.sort(
            key=lambda item: (item["confidence"], item["path"] == cls.DEFAULT_ENTRY_PATH),
            reverse=True,
        )
        if len(valid_candidates) == 1 or (
            len(valid_candidates) > 1
            and valid_candidates[0]["confidence"] > valid_candidates[1]["confidence"]
        ):
            selected, discovery_status = valid_candidates[0], "one_candidate"
        elif len(valid_candidates) > 1:
            selected, discovery_status = None, "multiple_candidates"
        elif cls.repo_appears_clean(workspace):
            selected, discovery_status = None, "clean_repo"
        else:
            selected, discovery_status = None, "no_candidate"
        return {
            "status": "ok",
            "discovery_status": discovery_status,
            "head_sha": head_sha,
            "stale": False,
            "selected_entry_path": selected["path"] if selected else None,
            "candidates": candidates,
            "sidecar": cls.sidecar_status(workspace, selected["path"]) if selected else None,
            "events": events
            + [{"code": discovery_status, "severity": "info" if valid_candidates else "warn"}],
            "error": None,
        }

    @staticmethod
    def current_head(workspace: str) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    @staticmethod
    def sidecar_status(workspace: str, entry_path: str) -> dict[str, Any]:
        sidecar_write_dir = str(Path(entry_path).parent / "agent_inc")
        sidecar_main_path = str(Path(sidecar_write_dir) / "main.beancount")
        include_line = SafePathService.include_line_for_entry(entry_path, sidecar_main_path)
        try:
            configured = include_line in Path(workspace, entry_path).read_text(encoding="utf-8")
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
        result = subprocess.run(
            [Beancount._bean_bin(workspace, "bean-check"), str(Path(workspace, entry_path))],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0, result.stdout + result.stderr

    @staticmethod
    def account_count(workspace: str, entry_path: str) -> tuple[bool, int]:
        result = subprocess.run(
            [
                Beancount._bean_bin(workspace, "bean-query"),
                "-f",
                "csv",
                str(Path(workspace, entry_path)),
                "SELECT DISTINCT account ORDER BY account",
            ],
            cwd=workspace,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return False, 0
        return True, len(list(csv.DictReader(io.StringIO(result.stdout))))

    @classmethod
    def _discover_candidates(
        cls,
        workspace: str,
        *,
        bean_check: Callable[[str, str], tuple[bool, str]],
        account_count: Callable[[str, str], tuple[bool, int]],
    ) -> list[dict[str, Any]]:
        cheap_candidates = [
            cls._cheap_candidate_for_path(workspace, path)
            for path in cls._candidate_paths(workspace)
        ]
        cheap_candidates.sort(
            key=lambda item: (item["confidence"], item["path"] == cls.DEFAULT_ENTRY_PATH),
            reverse=True,
        )
        validated: list[dict[str, Any]] = []
        for index, candidate in enumerate(cheap_candidates):
            if index < cls.MAX_DISCOVERY_VALIDATIONS:
                validated.append(
                    cls._validated_candidate_for_path(
                        workspace,
                        candidate["path"],
                        base_candidate=candidate,
                        bean_check=bean_check,
                        account_count=account_count,
                    )
                )
            else:
                skipped = dict(candidate)
                skipped["validation"] = {"status": "not_checked", "account_count": 0}
                validated.append(skipped)
        return validated

    @classmethod
    def _candidate_paths(cls, workspace: str) -> list[str]:
        all_files = cls._beancount_files(workspace)
        hinted = {path for path in all_files if Path(path).name.lower() in cls.ROOT_NAME_HINTS}
        return sorted(hinted | set(cls._rg_candidate_files(workspace)))

    @staticmethod
    def _beancount_files(workspace: str) -> list[str]:
        root = Path(workspace)
        return sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*.beancount")
            if ".git" not in path.parts
        )

    @classmethod
    def _rg_candidate_files(cls, workspace: str) -> list[str]:
        pattern = r'option\s+"|^\s*include\s+"|^\d{4}-\d{2}-\d{2}\s+open\s+'
        try:
            result = subprocess.run(
                ["rg", "-l", "--glob", "*.beancount", pattern, "."],
                cwd=workspace,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            return cls._python_candidate_files(workspace)
        if result.returncode not in {0, 1}:
            return cls._python_candidate_files(workspace)
        return sorted(
            line.removeprefix("./") for line in result.stdout.splitlines() if line.strip()
        )

    @classmethod
    def _python_candidate_files(cls, workspace: str) -> list[str]:
        matches: list[str] = []
        for path in cls._beancount_files(workspace):
            try:
                content = Path(workspace, path).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = Path(workspace, path).read_text(errors="ignore")
            except OSError:
                continue
            if 'option "' in content or "\ninclude " in f"\n{content}" or " open " in content:
                matches.append(path)
        return sorted(matches)

    @staticmethod
    def repo_appears_clean(workspace: str) -> bool:
        for child in Path(workspace).iterdir():
            if child.name == ".git":
                continue
            if child.is_dir() and not any(child.rglob("*")):
                continue
            return False
        return True

    @classmethod
    def _cheap_candidate_for_path(cls, workspace: str, path: str) -> dict[str, Any]:
        file_path = Path(workspace, path)
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = file_path.read_text(errors="ignore")
        except OSError:
            content = ""
        reasons: list[str] = []
        confidence = 0
        if file_path.name.lower() in cls.ROOT_NAME_HINTS:
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

    @classmethod
    def _validated_candidate_for_path(
        cls,
        workspace: str,
        path: str,
        *,
        base_candidate: dict[str, Any] | None = None,
        bean_check: Callable[[str, str], tuple[bool, str]] | None = None,
        account_count: Callable[[str, str], tuple[bool, int]] | None = None,
    ) -> dict[str, Any]:
        candidate = base_candidate or cls._cheap_candidate_for_path(workspace, path)
        ok, _ = (bean_check or cls.bean_check)(workspace, path)
        accounts_listed, count = (
            (account_count or cls.account_count)(workspace, path) if ok else (False, 0)
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
                "account_count": count,
            },
        }
