"""Unit tests for LedgerService."""

from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_core.services.ledger import (
    Beancount,
    LedgerService,
    LedgerServiceError,
)
from agent_core.services.types import (
    CommitResult,
    DependencyUnavailable,
    InvariantViolation,
    LedgerConfig,
    Preview,
    QueryResult,
    ValidationFailed,
)

TXN = '2026-06-15 * "Dinner"\n  Expenses:Food:Dining  100 CNY\n  Assets:Cash          -100 CNY'


@pytest.fixture
def git_service() -> Mock:
    service = Mock()
    service.commit_and_push.return_value = {
        "ok": True,
        "error": None,
        "push": "PUSHED: ok",
    }
    return service


@pytest.fixture
def custom_ledger_workspace(tmp_path: Path) -> tuple[Path, LedgerConfig]:
    books = tmp_path / "books"
    sidecar = books / "agent_sidecar"
    sidecar.mkdir(parents=True)
    config = LedgerConfig(
        entry_path="books/root.beancount",
        sidecar_main_path="books/agent_sidecar/main.beancount",
        sidecar_write_dir="books/agent_sidecar",
    )
    month = date.today().strftime("%Y-%m")
    (books / "root.beancount").write_text(
        'option "title" "Custom Ledger"\n'
        'option "operating_currency" "CNY"\n'
        'include "agent_sidecar/main.beancount"\n'
    )
    (sidecar / "main.beancount").write_text(
        "2020-01-01 open Assets:Cash CNY\n"
        "2020-01-01 open Expenses:Food:Dining CNY\n"
        "2020-01-01 open Equity:Opening-Balances CNY\n"
        f'include "{month}.beancount"\n'
    )
    (sidecar / f"{month}.beancount").write_text(
        '2020-01-01 * "Opening balance"\n'
        "  Assets:Cash              1000 CNY\n"
        "  Expenses:Food:Dining        0 CNY\n"
        "  Equity:Opening-Balances -1000 CNY\n"
    )
    return tmp_path, config


def test_preview_commit_validates_accounts(ledger_workspace: Path) -> None:
    result = LedgerService().preview_commit(
        str(ledger_workspace), TXN, "record dinner", ["Expenses:Food", "Assets:Cash"]
    )

    assert isinstance(result, Preview)
    assert result.preview["accounts_validated"] == [
        "Assets:Cash",
        "Expenses:Food:Dining",
    ]


def test_preview_commit_rejects_unknown_account(ledger_workspace: Path) -> None:
    result = LedgerService().preview_commit(
        str(ledger_workspace),
        TXN.replace("Expenses:Food:Dining", "Expenses:Unknown"),
        "bad",
    )

    assert isinstance(result, InvariantViolation)
    assert result.invariant == "ACCOUNT_WHITELIST"


def test_confirm_commit_writes_formats_and_pushes(
    ledger_workspace: Path, git_service: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    formatted: list[str] = []
    monkeypatch.setattr(Beancount, "bean_format", lambda _workspace, path: formatted.append(path))

    result = LedgerService().confirm_commit(
        str(ledger_workspace), TXN, "record dinner", "repo", git_service
    )

    assert isinstance(result, CommitResult)
    target = ledger_workspace / result.result["target_file"]
    assert "Dinner" in target.read_text()
    assert formatted == [str(target)]
    git_service.commit_and_push.assert_called_once()


def test_confirm_commit_uses_configured_sidecar_paths(
    custom_ledger_workspace: tuple[Path, LedgerConfig],
    git_service: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config = custom_ledger_workspace
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)

    result = LedgerService().confirm_commit(
        str(workspace),
        TXN,
        "record dinner",
        "repo",
        git_service,
        ledger_config=config,
    )

    assert isinstance(result, CommitResult)
    assert result.result["target_file"].startswith("books/agent_sidecar/")
    assert "Dinner" in (workspace / result.result["target_file"]).read_text()
    assert not (workspace / "data").exists()


def test_confirm_commit_reverts_invalid_transaction(
    ledger_workspace: Path, git_service: Mock
) -> None:
    result = LedgerService().confirm_commit(
        str(ledger_workspace),
        '2026-06-15 * "Bad"\n  Expenses:Food:Dining  100 CNY',
        "bad",
        "repo",
        git_service,
    )

    assert isinstance(result, ValidationFailed)
    target = ledger_workspace / "data" / "agent_inc" / date.today().strftime("%Y-%m.beancount")
    assert "Bad" not in target.read_text()
    git_service.commit_and_push.assert_not_called()


def test_confirm_commit_reports_git_failure(ledger_workspace: Path, git_service: Mock) -> None:
    git_service.commit_and_push.return_value = {
        "ok": False,
        "error": "commit failed",
        "push": None,
    }

    result = LedgerService().confirm_commit(
        str(ledger_workspace), TXN, "record dinner", "repo", git_service
    )

    assert isinstance(result, DependencyUnavailable)


def test_open_preview_and_confirm(
    ledger_workspace: Path, git_service: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    service = LedgerService()

    preview = service.preview_open(
        str(ledger_workspace), "Assets:Bank:Savings", "CNY", "2026-06-15", "Savings"
    )
    result = service.confirm_open(
        str(ledger_workspace),
        "Assets:Bank:Savings",
        "CNY",
        "2026-06-15",
        "repo",
        git_service,
        "Savings",
    )

    assert isinstance(preview, Preview)
    assert isinstance(result, CommitResult)
    assert (
        "Assets:Bank:Savings"
        in (ledger_workspace / "data" / "agent_inc" / "main.beancount").read_text()
    )


def test_open_rejects_bad_name_and_existing_account(ledger_workspace: Path) -> None:
    service = LedgerService()
    bad = service.preview_open(str(ledger_workspace), "assets:cash", None, "2026-06-15")
    duplicate = service.preview_open(str(ledger_workspace), "Assets:Cash", None, "2026-06-15")

    assert isinstance(bad, InvariantViolation)
    assert bad.invariant == "ACCOUNT_NAME_FORMAT"
    assert isinstance(duplicate, InvariantViolation)
    assert duplicate.invariant == "ACCOUNT_ALREADY_EXISTS"


def test_update_preview_and_confirm(
    ledger_workspace: Path, git_service: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    replacement = (
        '2026-05-12 * "Lunch"\n  Expenses:Food:Dining  95 CNY\n  Assets:Cash          -95 CNY'
    )
    service = LedgerService()

    preview = service.preview_update(
        str(ledger_workspace), "2026-05-12", "Lunch", replacement, "update lunch"
    )
    result = service.confirm_update(
        str(ledger_workspace),
        "2026-05-12",
        "Lunch",
        replacement,
        "update lunch",
        "repo",
        git_service,
    )

    assert isinstance(preview, Preview)
    assert preview.preview["advisory"]["warning"] == "VALUE_CHANGED"
    assert isinstance(result, CommitResult)
    assert "95 CNY" in (ledger_workspace / result.result["file"]).read_text()


def test_update_reports_missing_transaction(ledger_workspace: Path) -> None:
    result = LedgerService().preview_update(
        str(ledger_workspace), "2026-05-12", "Missing", TXN, "update"
    )

    assert isinstance(result, InvariantViolation)
    assert result.invariant == "TRANSACTION_NOT_FOUND"


def test_bulk_preview_and_confirm(
    ledger_workspace: Path, git_service: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    service = LedgerService()

    preview = service.preview_bulk(str(ledger_workspace), TXN, "bulk")
    result = service.confirm_bulk(str(ledger_workspace), TXN, "bulk", "repo", git_service)

    assert isinstance(preview, Preview)
    assert preview.preview["transaction_count"] == 1
    assert isinstance(result, CommitResult)


def test_bulk_supports_staging_file(
    ledger_workspace: Path, git_service: Mock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    staging = tmp_path / "staged.beancount"
    staging.write_text(TXN)

    result = LedgerService().confirm_bulk(
        str(ledger_workspace),
        commit_message="bulk",
        repo_url="repo",
        git_service=git_service,
        transactions_file=str(staging),
    )

    assert isinstance(result, CommitResult)
    assert not staging.exists()
    target = ledger_workspace / result.result["target_file"]
    assert "Dinner" in target.read_text()


def test_staged_bulk_commits_the_content_that_was_validated(
    ledger_workspace: Path, git_service: Mock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    staging = tmp_path / "staged.beancount"
    staging.write_text(TXN)
    service = LedgerService()
    original_preview = service.preview_bulk

    def mutate_after_validation(*args, **kwargs):
        result = original_preview(*args, **kwargs)
        staging.write_text(
            '2026-06-15 * "Unvalidated"\n'
            "  Expenses:Unknown  100 CNY\n"
            "  Assets:Cash      -100 CNY"
        )
        return result

    monkeypatch.setattr(service, "preview_bulk", mutate_after_validation)
    result = service.confirm_bulk(
        str(ledger_workspace),
        commit_message="bulk",
        repo_url="repo",
        git_service=git_service,
        transactions_file=str(staging),
    )

    assert isinstance(result, CommitResult)
    target = ledger_workspace / result.result["target_file"]
    assert "Dinner" in target.read_text()
    assert "Unvalidated" not in target.read_text()


def test_read_operations_return_typed_results(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_rows(_workspace: str, bql: str, *_args):
        calls.append(bql)
        if "sum(position)" in bql:
            return [{"balance": "123 CNY"}], None
        return [{"account": "Assets:Cash"}], None

    monkeypatch.setattr(Beancount, "run_bql_rows", fake_rows)
    service = LedgerService()

    balance = service.get_balance(str(ledger_workspace), "Assets:Cash", "2026-06-01")
    found = service.find_transactions(str(ledger_workspace), narration_contains="Lunch")
    queried = service.query_bql(str(ledger_workspace), "SELECT account")

    assert balance.balance == "123 CNY"
    assert found.count == 1
    assert queried.count == 1
    assert all(isinstance(result, QueryResult) for result in (balance, found, queried))
    assert len(calls) == 3


def test_query_template_and_preflight_report(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "test.bql").write_text("-- description: test\nSELECT {column}")

    def fake_rows(_workspace: str, bql: str, *_args):
        if "DISTINCT account" in bql:
            return [{"account": "Assets:Cash"}], None
        return [{"bql": bql}], None

    monkeypatch.setattr(Beancount, "run_bql_rows", fake_rows)

    queried = LedgerService.query_template(
        str(ledger_workspace), "test", {"column": "account"}, str(templates)
    )
    report = LedgerService.preflight_report(str(ledger_workspace))

    assert queried.status == "SUCCESS"
    assert queried.rows == [{"bql": "SELECT account"}]
    assert report.status == "CLEAN"
    assert "Assets:Cash" in report.accounts


def test_preflight_and_queries_use_configured_entry_path(
    custom_ledger_workspace: tuple[Path, LedgerConfig],
) -> None:
    workspace, config = custom_ledger_workspace

    report = LedgerService.preflight_report(str(workspace), config)
    accounts = LedgerService.get_accounts(str(workspace), config)
    balance = LedgerService.get_balance(str(workspace), "Assets:Cash", ledger_config=config)

    assert report.status == "CLEAN"
    assert report.target is not None
    assert report.target.startswith("books/agent_sidecar/")
    assert "Assets:Cash" in accounts
    assert "1000 CNY" in (balance.balance or "")


def test_get_accounts_raises_on_bql_error(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Beancount, "run_bql_rows", lambda *_args: ([], "broken"))

    with pytest.raises(LedgerServiceError, match="broken"):
        LedgerService.get_accounts(str(ledger_workspace))
