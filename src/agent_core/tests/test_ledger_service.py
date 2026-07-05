"""Unit tests for LedgerService."""

from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

import agent_core.services.ledger as ledger_module
from agent_core.services.ledger import (
    Beancount,
    LedgerService,
    LedgerServiceError,
)
from agent_core.services.types import (
    ApplyReceipt,
    ApprovalRequired,
    CommitResult,
    DependencyUnavailable,
    IntegrityFailed,
    InvariantViolation,
    LedgerConfig,
    PendingAction,
    Preview,
    QueryResult,
    ValidationFailed,
)

TXN = '2026-06-15 * "Dinner"\n  Expenses:Food:Dining  100 CNY\n  Assets:Cash          -100 CNY'
DEPENDENT_TXN = (
    '2026-06-16 * "Savings transfer"\n'
    "  Assets:Bank:Savings   100 CNY\n"
    "  Assets:Cash          -100 CNY"
)


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


def test_get_accounts_includes_open_only_accounts(ledger_workspace: Path) -> None:
    sidecar_main = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    sidecar_main.write_text(
        sidecar_main.read_text()
        + "\n2020-01-01 open Expenses:Tax:Federal USD\n"
    )

    accounts = LedgerService.get_accounts(str(ledger_workspace))

    assert "Expenses:Tax:Federal" in accounts


def test_bean_check_uses_in_process_loader_by_default(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Beancount.invalidate_workspace(str(ledger_workspace))

    def fail_subprocess_run(*_args, **_kwargs):
        raise AssertionError("bean_check should not shell out by default")

    monkeypatch.setattr("subprocess.run", fail_subprocess_run)

    ok, output = Beancount.bean_check(str(ledger_workspace))

    assert ok is True
    assert output == ""


def test_confirm_commit_invalidates_cached_validation_before_checking(
    ledger_workspace: Path, git_service: Mock
) -> None:
    ok, output = Beancount.bean_check(str(ledger_workspace))
    assert ok is True
    assert output == ""

    result = LedgerService().confirm_commit(
        str(ledger_workspace),
        '2026-06-15 * "Bad"\n  Expenses:Food:Dining  100 CNY',
        "bad",
        "repo",
        git_service,
    )

    assert isinstance(result, ValidationFailed)
    assert result.error == "transaction_not_balanced"
    assert result.advisory is not None
    assert result.advisory["error_type"] == "transaction_not_balanced"
    git_service.commit_and_push.assert_not_called()


def test_bean_check_fingerprint_detects_external_file_edits(
    ledger_workspace: Path,
) -> None:
    ok, output = Beancount.bean_check(str(ledger_workspace))
    assert ok is True
    assert output == ""

    sidecar = ledger_workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
    sidecar.write_text(
        sidecar.read_text()
        + '\n2026-06-16 * "External bad edit"\n'
        + "  Expenses:Food:Dining  100 CNY\n"
    )

    ok, output = Beancount.bean_check(str(ledger_workspace))

    assert ok is False
    assert "does not balance" in output


def test_prepare_commit_materializes_pending_action_contract(ledger_workspace: Path) -> None:
    result = LedgerService().prepare_commit(
        str(ledger_workspace), TXN, "record dinner", ["Expenses:Food", "Assets:Cash"]
    )

    assert isinstance(result, PendingAction)
    assert isinstance(result, ApprovalRequired)
    assert result.status == "PENDING_ACTION"
    assert result.action_type == "commit_transaction"
    assert result.execution_spec["transaction_text"] == TXN
    assert result.display["diff"] == TXN
    assert result.digest
    assert result.signature == f"sha256:{result.digest}"
    assert result.policy["risk"] == "normal"
    assert result.policy["requires_elevated_review"] is False
    assert result.continue_after_approval is False
    assert result.continuation_reason == ""
    assert result.next_intent_summary == ""
    assert result.validation["dry_run"]["status"] == "validated"


def test_prepare_commit_dry_run_rejects_invalid_without_pending_action(
    ledger_workspace: Path,
) -> None:
    target = ledger_workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
    original = target.read_text()

    result = LedgerService().prepare_commit(
        str(ledger_workspace),
        '2026-06-15 * "Bad"\n  Expenses:Food:Dining  100 CNY',
        "bad",
    )

    assert isinstance(result, ValidationFailed)
    assert result.status == "VALIDATION_FAILED"
    assert result.error == "transaction_not_balanced"
    assert result.advisory is not None
    assert result.advisory["retryable"] is True
    assert "Bad" not in target.read_text()
    assert target.read_text() == original


def test_pending_action_integrity_detects_mutation(ledger_workspace: Path) -> None:
    result = LedgerService().prepare_commit(str(ledger_workspace), TXN, "record dinner")
    assert isinstance(result, PendingAction)

    payload = result.__dict__.copy()
    payload["execution_spec"] = {
        **result.execution_spec,
        "transaction_text": TXN.replace("Dinner", "Tampered"),
    }

    integrity = LedgerService.verify_pending_action(payload)
    assert isinstance(integrity, IntegrityFailed)


def test_pending_action_integrity_covers_continuation_fields(ledger_workspace: Path) -> None:
    result = LedgerService().prepare_commit(str(ledger_workspace), TXN, "record dinner")
    assert isinstance(result, PendingAction)

    payload = result.__dict__.copy()
    payload["continue_after_approval"] = True
    payload["continuation_reason"] = (
        "Need approval before the dependent transaction can be planned."
    )
    payload["next_intent_summary"] = "Record the dependent transaction after approval."

    integrity = LedgerService.verify_pending_action(payload)
    assert isinstance(integrity, IntegrityFailed)


def test_apply_pending_action_uses_exact_contract(
    ledger_workspace: Path, git_service: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    pending = LedgerService().prepare_commit(str(ledger_workspace), TXN, "record dinner")
    assert isinstance(pending, PendingAction)

    result = LedgerService().apply_pending_action(
        str(ledger_workspace),
        pending.__dict__.copy(),
        "repo",
        git_service,
    )

    assert isinstance(result, ApplyReceipt)
    assert result.pending_action_id == pending.pending_action_id
    target = ledger_workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
    assert "Dinner" in target.read_text()


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
    assert result.error == "transaction_not_balanced"
    target = ledger_workspace / "data" / "agent_inc" / date.today().strftime("%Y-%m.beancount")
    assert "Bad" not in target.read_text()
    git_service.commit_and_push.assert_not_called()


def test_apply_pending_action_revalidates_and_rejects_stale_invalid_contract(
    ledger_workspace: Path, git_service: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = LedgerService().prepare_commit(str(ledger_workspace), TXN, "record dinner")
    assert isinstance(pending, PendingAction)

    def pass_integrity(_action):
        return None

    monkeypatch.setattr(LedgerService, "verify_pending_action", staticmethod(pass_integrity))
    payload = pending.__dict__.copy()
    payload["execution_spec"] = {
        **pending.execution_spec,
        "transaction_text": '2026-06-15 * "Bad"\n  Expenses:Food:Dining  100 CNY',
    }

    result = LedgerService().apply_pending_action(
        str(ledger_workspace),
        payload,
        "repo",
        git_service,
    )

    assert isinstance(result, ValidationFailed)
    assert result.error == "transaction_not_balanced"
    target = ledger_workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
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


def test_prepare_open_materializes_pending_action_and_apply(
    ledger_workspace: Path, git_service: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    target = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    original = target.read_text()

    pending = LedgerService().prepare_open(
        str(ledger_workspace),
        "Assets:Bank:Savings",
        "CNY",
        "2026-06-15",
        "Savings",
    )

    assert isinstance(pending, PendingAction)
    assert isinstance(pending, ApprovalRequired)
    assert pending.action_type == "open_account"
    assert pending.execution_spec["account_name"] == "Assets:Bank:Savings"
    assert pending.display["kind"] == "account_open_preview"
    assert target.read_text() == original

    result = LedgerService().apply_pending_action(
        str(ledger_workspace),
        pending.__dict__.copy(),
        "repo",
        git_service,
    )

    assert isinstance(result, ApplyReceipt)
    assert result.action_type == "open_account"
    assert "Assets:Bank:Savings" in target.read_text()


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


def test_prepare_update_materializes_pending_action_contract(
    ledger_workspace: Path,
) -> None:
    replacement = (
        '2026-05-12 * "Lunch"\n  Expenses:Food:Dining  95 CNY\n  Assets:Cash          -95 CNY'
    )

    result = LedgerService().prepare_update(
        str(ledger_workspace), "2026-05-12", "Lunch", replacement, "update lunch"
    )

    assert isinstance(result, PendingAction)
    assert result.action_type == "update_transaction"
    assert result.execution_spec["target_date"] == "2026-05-12"
    assert result.execution_spec["new_transaction_text"] == replacement
    assert result.validation["dry_run"]["status"] == "validated"
    assert result.policy["risk"] == "elevated"


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


def test_prepare_bulk_materializes_pending_action_contract(
    ledger_workspace: Path,
) -> None:
    result = LedgerService().prepare_bulk(str(ledger_workspace), TXN, "bulk")

    assert isinstance(result, PendingAction)
    assert result.action_type == "bulk_commit"
    assert result.execution_spec["transactions_text"] == TXN
    assert result.validation["transaction_count"] == 1
    assert result.validation["dry_run"]["status"] == "validated"


def test_prepare_change_set_validates_dependent_open_and_transaction_without_write(
    ledger_workspace: Path,
) -> None:
    sidecar_main = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    month_file = ledger_workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
    original_main = sidecar_main.read_text()
    original_month = month_file.read_text()

    result = LedgerService().prepare_change_set(
        str(ledger_workspace),
        [
            {
                "type": "open_account",
                "account_name": "Assets:Bank:Savings",
                "currency": "CNY",
                "open_date": "2026-06-16",
                "display_name": "Savings",
            },
            {
                "type": "commit_transaction",
                "transaction_text": DEPENDENT_TXN,
            },
        ],
        "record savings transfer",
    )

    assert isinstance(result, PendingAction)
    assert result.action_type == "change_set"
    assert result.execution_spec["commit_message"] == "record savings transfer"
    assert result.validation["operation_count"] == 2
    assert result.validation["transaction_count"] == 1
    assert "Assets:Bank:Savings" in result.validation["accounts"]
    assert result.display["kind"] == "change_set_preview"
    assert len(result.display["items"]) == 2
    assert sidecar_main.read_text() == original_main
    assert month_file.read_text() == original_month


def test_prepare_change_set_reports_operation_index_for_unknown_account(
    ledger_workspace: Path,
) -> None:
    result = LedgerService().prepare_change_set(
        str(ledger_workspace),
        [
            {
                "type": "commit_transaction",
                "transaction_text": DEPENDENT_TXN,
            }
        ],
        "record savings transfer",
    )

    assert isinstance(result, InvariantViolation)
    assert result.invariant == "ACCOUNT_WHITELIST"
    assert result.detail["operation_index"] == 0
    assert result.provided == ["Assets:Bank:Savings"]


def test_apply_change_set_writes_both_changes_and_commits_once(
    ledger_workspace: Path, git_service: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    formatted: list[str] = []
    monkeypatch.setattr(Beancount, "bean_format", lambda _workspace, path: formatted.append(path))
    pending = LedgerService().prepare_change_set(
        str(ledger_workspace),
        [
            {
                "type": "open_account",
                "account_name": "Assets:Bank:Savings",
                "currency": "CNY",
                "open_date": "2026-06-16",
            },
            {
                "type": "commit_transaction",
                "transaction_text": DEPENDENT_TXN,
            },
        ],
        "record savings transfer",
    )
    assert isinstance(pending, PendingAction)

    result = LedgerService().apply_pending_action(
        str(ledger_workspace),
        pending.__dict__.copy(),
        "repo",
        git_service,
    )

    assert isinstance(result, ApplyReceipt)
    assert result.action_type == "change_set"
    assert "Assets:Bank:Savings" in (
        ledger_workspace / "data" / "agent_inc" / "main.beancount"
    ).read_text()
    assert "Savings transfer" in (
        ledger_workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
    ).read_text()
    assert len(formatted) == 2
    git_service.commit_and_push.assert_called_once()


def test_apply_change_set_validation_failure_leaves_no_partial_write(
    ledger_workspace: Path, git_service: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = LedgerService().prepare_change_set(
        str(ledger_workspace),
        [
            {
                "type": "open_account",
                "account_name": "Assets:Bank:Savings",
                "currency": "CNY",
                "open_date": "2026-06-16",
            },
            {
                "type": "commit_transaction",
                "transaction_text": DEPENDENT_TXN,
            },
        ],
        "record savings transfer",
    )
    assert isinstance(pending, PendingAction)
    sidecar_main = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    month_file = ledger_workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
    original_main = sidecar_main.read_text()
    original_month = month_file.read_text()

    payload = pending.__dict__.copy()
    payload["execution_spec"] = {
        **pending.execution_spec,
        "operations": [
            *pending.execution_spec["operations"][:1],
            {
                "type": "commit_transaction",
                "transaction_text": (
                    '2026-06-16 * "Bad transfer"\n'
                    "  Assets:Bank:Savings   100 CNY\n"
                ),
            },
        ],
    }
    monkeypatch.setattr(LedgerService, "verify_pending_action", staticmethod(lambda _action: None))

    result = LedgerService().apply_pending_action(
        str(ledger_workspace),
        payload,
        "repo",
        git_service,
    )

    assert isinstance(result, ValidationFailed)
    assert sidecar_main.read_text() == original_main
    assert month_file.read_text() == original_month
    git_service.commit_and_push.assert_not_called()


def test_apply_change_set_commit_failure_restores_sidecar_files(
    ledger_workspace: Path, git_service: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    git_service.commit_and_push.return_value = {
        "ok": False,
        "error": "commit failed",
        "push": None,
    }
    pending = LedgerService().prepare_change_set(
        str(ledger_workspace),
        [
            {
                "type": "open_account",
                "account_name": "Assets:Bank:Savings",
                "currency": "CNY",
                "open_date": "2026-06-16",
            },
            {
                "type": "commit_transaction",
                "transaction_text": DEPENDENT_TXN,
            },
        ],
        "record savings transfer",
    )
    assert isinstance(pending, PendingAction)
    sidecar_main = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    month_file = ledger_workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
    original_main = sidecar_main.read_text()
    original_month = month_file.read_text()

    result = LedgerService().apply_pending_action(
        str(ledger_workspace),
        pending.__dict__.copy(),
        "repo",
        git_service,
    )

    assert isinstance(result, DependencyUnavailable)
    assert sidecar_main.read_text() == original_main
    assert month_file.read_text() == original_month


def test_apply_change_set_copy_failure_restores_partial_sidecar_copy(
    ledger_workspace: Path, git_service: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    pending = LedgerService().prepare_change_set(
        str(ledger_workspace),
        [
            {
                "type": "open_account",
                "account_name": "Assets:Bank:Savings",
                "currency": "CNY",
                "open_date": "2026-06-16",
            },
            {
                "type": "commit_transaction",
                "transaction_text": DEPENDENT_TXN,
            },
        ],
        "record savings transfer",
    )
    assert isinstance(pending, PendingAction)
    sidecar_main = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    month_file = ledger_workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
    original_main = sidecar_main.read_text()
    original_month = month_file.read_text()
    original_write_repo_file = ledger_module._write_repo_file
    calls = 0

    def fail_second_write(workspace: str, rel_path: str, content: str | None) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("copy failed")
        original_write_repo_file(workspace, rel_path, content)

    monkeypatch.setattr(ledger_module, "_write_repo_file", fail_second_write)

    with pytest.raises(LedgerServiceError):
        LedgerService().apply_pending_action(
            str(ledger_workspace),
            pending.__dict__.copy(),
            "repo",
            git_service,
        )

    assert sidecar_main.read_text() == original_main
    assert month_file.read_text() == original_month
    git_service.commit_and_push.assert_not_called()


def test_prepare_change_set_bulk_sized_transaction_set_is_high_risk(
    ledger_workspace: Path,
) -> None:
    operations = [
        {
            "type": "commit_transaction",
            "transaction_text": (
                f'2026-06-{day:02d} * "Small purchase {day}"\n'
                "  Expenses:Food:Dining    1 CNY\n"
                "  Assets:Cash            -1 CNY"
            ),
        }
        for day in range(1, 26)
    ]

    result = LedgerService().prepare_change_set(
        str(ledger_workspace),
        operations,
        "record many small purchases",
    )

    assert isinstance(result, PendingAction)
    assert result.action_type == "change_set"
    assert result.validation["transaction_count"] == 25
    assert result.policy["risk"] == "high"
    assert result.policy["requires_elevated_review"] is True


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


def test_ledger_config_derives_sidecar_main_from_write_dir() -> None:
    config = LedgerConfig(
        entry_path="books/root.beancount",
        sidecar_main_path=None,
        sidecar_write_dir="books/agent_sidecar",
    )

    assert config.sidecar_main_path == "books/agent_sidecar/main.beancount"


def test_ledger_config_replaces_legacy_monthly_sidecar_main_path() -> None:
    config = LedgerConfig(
        entry_path="main.beancount",
        sidecar_main_path="data/agent_inc/2026-06.beancount",
        sidecar_write_dir="data/agent_inc",
    )

    assert config.sidecar_main_path == "data/agent_inc/main.beancount"


def test_get_accounts_raises_on_bql_error(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Beancount, "run_bql_rows", lambda *_args: ([], "broken"))

    with pytest.raises(LedgerServiceError, match="broken"):
        LedgerService.get_accounts(str(ledger_workspace))
