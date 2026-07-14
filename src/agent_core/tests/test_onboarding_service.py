import subprocess
from pathlib import Path
from unittest.mock import Mock

from agent_core.services.onboarding import OnboardingService
from agent_core.services.onboarding.discovery import OnboardingDiscoveryService
from agent_core.services.onboarding.paths import SafePathService
from agent_core.services.onboarding.setup import OnboardingSetupService


def test_discover_clean_repo(tmp_path: Path) -> None:
    result = OnboardingService.discover(str(tmp_path))

    assert result["discovery_status"] == "clean_repo"
    assert result["candidates"] == []


def test_discover_no_candidate_for_non_clean_repo(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("not a ledger")

    result = OnboardingService.discover(str(tmp_path))

    assert result["discovery_status"] == "no_candidate"


def test_discover_multiple_candidates(tmp_path: Path) -> None:
    data = tmp_path / "books"
    data.mkdir()
    for name in ["alpha.beancount", "beta.beancount"]:
        (data / name).write_text(
            'option "title" "Test"\n'
            'option "operating_currency" "USD"\n'
            "2020-01-01 open Assets:Cash USD\n",
        )

    result = OnboardingService.discover(str(tmp_path))

    assert result["discovery_status"] == "multiple_candidates"


def test_discovery_validates_only_top_candidates(tmp_path: Path, monkeypatch) -> None:
    books = tmp_path / "books"
    books.mkdir()
    for index in range(12):
        (books / f"ledger-{index:02d}.beancount").write_text(
            'option "title" "Test"\n'
            'option "operating_currency" "USD"\n'
            "2020-01-01 open Assets:Cash USD\n",
        )
    checked: list[str] = []

    def fake_check(_workspace: str, entry_path: str):
        checked.append(entry_path)
        return True, ""

    monkeypatch.setattr(OnboardingService, "bean_check", fake_check)
    monkeypatch.setattr(OnboardingService, "account_count", lambda *_args: (True, 1))

    result = OnboardingService.discover(str(tmp_path))

    assert len(checked) == OnboardingService.MAX_DISCOVERY_VALIDATIONS
    assert any(
        candidate["validation"]["status"] == "not_checked" for candidate in result["candidates"]
    )


def test_discover_rejects_path_traversal(ledger_workspace: Path) -> None:
    result = OnboardingService.discover(str(ledger_workspace), entry_path="../main.beancount")

    assert result["discovery_status"] == "invalid_request"
    assert result["error"]["code"] == "PATH_TRAVERSAL"


def test_discover_one_candidate_and_sidecar(ledger_workspace: Path) -> None:
    result = OnboardingService.discover(str(ledger_workspace))

    assert result["discovery_status"] == "one_candidate"
    assert result["selected_entry_path"] == "data/main.beancount"
    assert result["sidecar"]["status"] == "configured"


def test_install_sidecar_preview_does_not_mutate(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    entry = data / "main.beancount"
    entry.write_text('option "title" "Test"\n')

    result = OnboardingService.preview_setup(
        str(tmp_path),
        operation="install_sidecar",
        entry_path="data/main.beancount",
    )

    assert result["status"] == "preview"
    assert 'include "agent_inc/main.beancount"' not in entry.read_text()
    assert not (data / "agent_inc" / "main.beancount").exists()


def test_setup_preview_rejects_path_traversal(ledger_workspace: Path) -> None:
    result = OnboardingService.preview_setup(
        str(ledger_workspace),
        operation="install_sidecar",
        entry_path="../main.beancount",
    )

    assert result["status"] == "error"
    assert result["code"] == "PATH_TRAVERSAL"


def test_setup_preview_rejects_entry_sidecar_alias(tmp_path: Path) -> None:
    result = OnboardingService.preview_setup(
        str(tmp_path),
        operation="initialize_ledger",
        entry_path="data/main.beancount",
        sidecar_main_path="data/main.beancount",
    )

    assert result["status"] == "error"
    assert result["code"] == "SETUP_PATH_ALIAS"


def test_discover_reports_missing_sidecar(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "main.beancount").write_text(
        'option "title" "Test"\n'
        'option "operating_currency" "USD"\n'
        "2020-01-01 open Assets:Cash USD\n",
    )

    result = OnboardingService.discover(str(tmp_path), entry_path="data/main.beancount")

    assert result["sidecar"]["status"] == "missing"


def test_confirm_rejects_stale_head(ledger_workspace: Path) -> None:
    result = OnboardingService.confirm_setup(
        str(ledger_workspace),
        operation="install_sidecar",
        expected_head_sha="not-current",
        repo_url="ignored",
        git_service=Mock(),
        token=None,
        entry_path="data/main.beancount",
    )

    assert result["status"] == "stale"
    assert result["code"] == "STALE_REPOSITORY"


class FakeGit:
    def __init__(self, fail_push: bool = False):
        self.fail_push = fail_push

    def push(self, workspace: str, repo_url: str, token: str | None = None) -> str:
        if self.fail_push:
            raise RuntimeError("push failed")
        return "PUSHED: ok"


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def test_confirm_initialize_ledger_clean_repo(tmp_path: Path) -> None:
    init_repo(tmp_path)

    result = OnboardingService.confirm_setup(
        str(tmp_path),
        operation="initialize_ledger",
        expected_head_sha="",
        repo_url="ignored",
        git_service=FakeGit(),
        token=None,
    )

    assert result["status"] == "success"
    assert (tmp_path / "data" / "main.beancount").exists()
    assert (tmp_path / "data" / "agent_inc" / "main.beancount").exists()


def test_setup_service_confirms_initialize_ledger_directly(tmp_path: Path) -> None:
    init_repo(tmp_path)

    result = OnboardingSetupService.confirm(
        str(tmp_path),
        operation="initialize_ledger",
        expected_head_sha="",
        repo_url="ignored",
        git_service=FakeGit(),
        token=None,
    )

    assert result["status"] == "success"
    assert (tmp_path / "data" / "main.beancount").exists()


def test_initialize_ledger_uses_selected_title_and_currency(tmp_path: Path) -> None:
    init_repo(tmp_path)

    preview = OnboardingService.preview_setup(
        str(tmp_path),
        operation="initialize_ledger",
        ledger_title="Family Books",
        operating_currency="cny",
    )

    assert preview["status"] == "preview"
    assert preview["ledger_title"] == "Family Books"
    assert preview["operating_currency"] == "CNY"

    result = OnboardingService.confirm_setup(
        str(tmp_path),
        operation="initialize_ledger",
        expected_head_sha="",
        repo_url="ignored",
        git_service=FakeGit(),
        token=None,
        ledger_title="Family Books",
        operating_currency="cny",
    )

    assert result["status"] == "success"
    entry = (tmp_path / "data" / "main.beancount").read_text()
    assert 'option "title" "Family Books"' in entry
    assert 'option "operating_currency" "CNY"' in entry


def test_install_sidecar_preview_does_not_include_clean_ledger_metadata(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    entry = data / "main.beancount"
    entry.write_text('option "title" "Existing"\n')

    result = OnboardingService.preview_setup(
        str(tmp_path),
        operation="install_sidecar",
        entry_path="data/main.beancount",
        ledger_title="Ignored",
        operating_currency="EUR",
    )

    assert result["status"] == "preview"
    assert result["ledger_title"] is None
    assert result["operating_currency"] is None


def test_initialize_rejects_non_clean_repo(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "README.md").write_text("existing content")

    result = OnboardingService.preview_setup(
        str(tmp_path),
        operation="initialize_ledger",
    )

    assert result["status"] == "error"
    assert result["code"] == "REPO_NOT_CLEAN"


def test_confirm_install_sidecar_existing_repo(tmp_path: Path) -> None:
    init_repo(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    entry = data / "main.beancount"
    entry.write_text(
        'option "title" "Test"\n'
        'option "operating_currency" "USD"\n'
        "2020-01-01 open Assets:Cash USD\n",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=tmp_path, check=True)
    head = OnboardingService.current_head(str(tmp_path))

    result = OnboardingService.confirm_setup(
        str(tmp_path),
        operation="install_sidecar",
        expected_head_sha=head,
        repo_url="ignored",
        git_service=FakeGit(),
        token=None,
        entry_path="data/main.beancount",
    )

    assert result["status"] == "success"
    assert 'include "agent_inc/main.beancount"' in entry.read_text()


def test_confirm_rolls_back_on_bean_check_failure(tmp_path: Path, monkeypatch) -> None:
    init_repo(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    entry = data / "main.beancount"
    original = (
        'option "title" "Test"\n'
        'option "operating_currency" "USD"\n'
        "2020-01-01 open Assets:Cash USD\n"
    )
    entry.write_text(original)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=tmp_path, check=True)
    head = OnboardingService.current_head(str(tmp_path))
    monkeypatch.setattr(OnboardingService, "bean_check", lambda *_args: (False, "bad"))

    result = OnboardingService.confirm_setup(
        str(tmp_path),
        operation="install_sidecar",
        expected_head_sha=head,
        repo_url="ignored",
        git_service=FakeGit(),
        token=None,
        entry_path="data/main.beancount",
    )

    assert result["status"] == "validation_failed"
    assert entry.read_text() == original


def test_confirm_reports_push_failure(tmp_path: Path) -> None:
    init_repo(tmp_path)

    result = OnboardingService.confirm_setup(
        str(tmp_path),
        operation="initialize_ledger",
        expected_head_sha="",
        repo_url="ignored",
        git_service=FakeGit(fail_push=True),
        token=None,
    )

    assert result["status"] == "dependency_unavailable"
    assert result["code"] == "GIT_PUSH_FAILED"


def test_onboarding_package_entry_points_preserve_facade_behavior(tmp_path: Path) -> None:
    assert OnboardingDiscoveryService.discover(str(tmp_path)) == OnboardingService.discover(
        str(tmp_path)
    )
    assert SafePathService.validate_repo_path(
        str(tmp_path), "data/main.beancount", must_exist=False
    ) == OnboardingService._validate_repo_path(
        str(tmp_path), "data/main.beancount", must_exist=False
    )
    preview = OnboardingSetupService.preview(str(tmp_path), operation="initialize_ledger")
    assert preview == OnboardingService.preview_setup(str(tmp_path), operation="initialize_ledger")
    assert preview["changes"] == [
        {"action": "create", "path": "data/main.beancount"},
        {"action": "create", "path": "data/agent_inc/main.beancount"},
    ]
