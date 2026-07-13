"""Failure-mode characterization for the shared mutation-plan coordinator."""

from __future__ import annotations

import inspect
import subprocess
from datetime import date
from pathlib import Path

import pytest

from agent_core.services.beancount import Beancount
from agent_core.services.ledger import LedgerService
from agent_core.services.mutations import (
    MutationApplier,
    MutationCoordinator,
    MutationExecutor,
    MutationPlanner,
    MutationValidator,
)
from agent_core.services.mutations.plans import MutationPlan
from agent_core.services.pending_actions import PendingActionService
from agent_core.services.types import ApplyReceipt

TXN = (
    '2026-06-15 * "Dinner"\n'
    "  Expenses:Food:Dining  100 CNY\n"
    "  Assets:Cash          -100 CNY"
)


class _CleanValidator:
    def check(self, _workspace: str, _config: object = None) -> tuple[bool, str]:
        return True, ""


class _NoopFormatter:
    def format(self, _workspace: str, _path: str) -> None:
        return None


class _SpyApplier(MutationApplier):
    def __init__(self) -> None:
        self.calls: list[tuple[str, MutationPlan]] = []

    def apply(self, workspace: str, plan: MutationPlan, config: object = None) -> tuple[str, ...]:
        self.calls.append((workspace, plan))
        return super().apply(workspace, plan, config)  # type: ignore[arg-type]


def test_validator_has_no_publish_capability_and_does_not_call_publisher(
    ledger_workspace: Path,
) -> None:
    """Validation receives only an applier and validator, never a Git publisher."""
    validator = MutationValidator(ledger_validator=_CleanValidator())

    result = validator.validate(str(ledger_workspace), MutationPlanner.commit(TXN, "record dinner"))

    assert result.validation.status == "validated"
    assert not hasattr(validator, "apply_and_publish")
    assert "publisher" not in inspect.signature(MutationValidator).parameters


def test_validator_and_executor_share_the_same_operation_applier(ledger_workspace: Path) -> None:
    """Dry-run and approval replay the plan through one injected applier."""
    applier = _SpyApplier()
    plan = MutationPlanner.commit(TXN, "record dinner")
    validator = MutationValidator(applier, _CleanValidator())
    executor = MutationExecutor(applier, _CleanValidator(), _NoopFormatter())
    publisher = type(
        "Publisher",
        (), {"commit_and_push": lambda *_args: {"ok": True, "push": "PUSHED"}},
    )()

    validated = validator.validate(str(ledger_workspace), plan)
    touched, git, failure = executor.apply_and_publish(
        str(ledger_workspace), plan, "repo", publisher
    )

    assert validated.touched_files == touched
    assert git["ok"] is True
    assert failure == ""
    assert len(applier.calls) == 2
    assert all(call_plan is plan for _, call_plan in applier.calls)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def test_apply_rolls_back_sidecar_when_formatter_fails(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Formatting is inside the same all-or-nothing sidecar boundary as apply."""
    main = ledger_workspace / "data/agent_inc/main.beancount"
    month = ledger_workspace / "data/agent_inc" / f"{date.today():%Y-%m}.beancount"
    before = {main: main.read_text(), month: month.read_text()}

    def formatter_failure(*_args: object) -> None:
        raise RuntimeError("bean-format failed")

    monkeypatch.setattr(Beancount, "bean_format", formatter_failure)
    git_service = type("Git", (), {"commit_and_push": lambda *_args: pytest.fail("publish")})()

    with pytest.raises(RuntimeError, match="bean-format failed"):
        MutationCoordinator().apply_and_publish(
            str(ledger_workspace),
            MutationPlanner.commit(TXN, "record dinner"),
            "repo",
            git_service,
        )

    assert {main: main.read_text(), month: month.read_text()} == before


def test_invalid_apply_removes_new_sidecar_files_and_directories(tmp_path: Path) -> None:
    """A failed validation cannot leave an untracked sidecar skeleton behind."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "main.beancount").write_text(
        'option "operating_currency" "CNY"\ninclude "agent_inc/main.beancount"\n'
    )

    _touched, git, failure = MutationCoordinator().apply_and_publish(
        str(tmp_path),
        MutationPlanner.commit(
            '2026-06-15 * "Unbalanced"\n  Expenses:Food:Dining  100 CNY', "bad"
        ),
        "repo",
        type("Git", (), {"commit_and_push": lambda *_args: pytest.fail("publish")})(),
    )

    assert git == {}
    assert failure
    assert not (data / "agent_inc").exists()


def test_publish_failure_restores_files_and_git_index(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A publisher that stages before failing cannot leak the staged mutation."""
    _git(["init"], ledger_workspace)
    _git(["config", "user.email", "test@example.com"], ledger_workspace)
    _git(["config", "user.name", "Test"], ledger_workspace)
    _git(["add", "data"], ledger_workspace)
    _git(["commit", "-m", "seed"], ledger_workspace)
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)

    class StagingFailure:
        def commit_and_push(self, workspace: str, *_args: object) -> dict[str, object]:
            _git(["add", "data"], Path(workspace))
            return {"ok": False, "error": "commit failed", "push": None}

    main = ledger_workspace / "data/agent_inc/main.beancount"
    month = ledger_workspace / "data/agent_inc" / f"{date.today():%Y-%m}.beancount"
    before = {main: main.read_text(), month: month.read_text()}
    _touched, git, failure = MutationCoordinator().apply_and_publish(
        str(ledger_workspace),
        MutationPlanner.commit(TXN, "record dinner"),
        "repo",
        StagingFailure(),
    )

    assert failure == ""
    assert git["ok"] is False
    assert {main: main.read_text(), month: month.read_text()} == before
    assert _git(["status", "--porcelain"], ledger_workspace).stdout == ""


def test_legacy_pending_action_without_a_mutation_plan_still_applies(
    ledger_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persisted pre-plan contracts remain readable during the schema transition."""
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    pending = PendingActionService.create_pending_action(
        action_type="commit_transaction",
        execution_spec={"transaction_text": TXN, "commit_message": "record dinner"},
        display={"diff": TXN},
        validation={"dry_run": {"status": "validated"}},
    )
    git_service = type(
        "Git",
        (),
        {"commit_and_push": lambda *_args: {"ok": True, "error": None, "push": "PUSHED"}},
    )()

    result = LedgerService().apply_pending_action(
        str(ledger_workspace), pending.__dict__.copy(), "repo", git_service
    )

    assert isinstance(result, ApplyReceipt)
    target = ledger_workspace / "data/agent_inc" / f"{date.today():%Y-%m}.beancount"
    assert "Dinner" in target.read_text()


@pytest.mark.parametrize(
    ("action_type", "prepare", "expected_text"),
    [
        (
            "open_account",
            lambda service, workspace: service.prepare_open(
                workspace, "Assets:Bank:Savings", "CNY", "2026-06-15", "Savings"
            ),
            "Assets:Bank:Savings",
        ),
        (
            "update_transaction",
            lambda service, workspace: service.prepare_update(
                workspace,
                "2026-05-12",
                "Lunch",
                '2026-05-12 * "Lunch"\n'
                "  Expenses:Food:Dining  95 CNY\n"
                "  Assets:Cash          -95 CNY",
                "update lunch",
            ),
            "95 CNY",
        ),
        (
            "bulk_commit",
            lambda service, workspace: service.prepare_bulk(workspace, TXN, "import dinner"),
            "Dinner",
        ),
        (
            "change_set",
            lambda service, workspace: service.prepare_change_set(
                workspace,
                [
                    {
                        "type": "open_account",
                        "account_name": "Assets:Bank:Savings",
                        "currency": "CNY",
                        "open_date": "2026-06-16",
                    },
                    {"type": "commit_transaction", "transaction_text": TXN},
                ],
                "open savings and record dinner",
            ),
            "Assets:Bank:Savings",
        ),
        (
            "balance_reconciliation",
            lambda service, workspace: service.prepare_balance_reconciliation(
                workspace,
                "2026-05-31",
                "Assets:Bank:Checking",
                "5120",
                "CNY",
                "Equity:Opening-Balances",
            ),
            "Balance reconciliation adjustment",
        ),
    ],
)
def test_signed_legacy_pending_actions_for_each_remaining_type_still_apply(
    ledger_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_type: str,
    prepare,
    expected_text: str,
) -> None:
    """All pre-plan persisted action types retain their explicit fallback path."""
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    prepared = prepare(LedgerService(), str(ledger_workspace))
    assert hasattr(prepared, "execution_spec")
    legacy_spec = {
        key: value
        for key, value in prepared.execution_spec.items()
        if key != "mutation_plan"
    }
    pending = PendingActionService.create_pending_action(
        action_type=action_type,
        execution_spec=legacy_spec,
        display=prepared.display,
        validation=prepared.validation,
    )
    git_service = type(
        "Git",
        (),
        {"commit_and_push": lambda *_args: {"ok": True, "error": None, "push": "PUSHED"}},
    )()

    result = LedgerService().apply_pending_action(
        str(ledger_workspace), pending.__dict__.copy(), "repo", git_service
    )

    assert isinstance(result, ApplyReceipt)
    assert result.action_type == action_type
    assert "mutation_plan" not in pending.execution_spec
    all_ledger_text = "\n".join(
        path.read_text() for path in (ledger_workspace / "data").rglob("*.beancount")
    )
    assert expected_text in all_ledger_text


@pytest.mark.parametrize(
    "spec",
    [
        {"version": 1, "operations": ["not-an-operation"], "preconditions": []},
        {"version": 1, "operations": [], "preconditions": ["not-a-precondition"]},
        {"version": 2, "operations": [], "preconditions": []},
    ],
)
def test_mutation_plan_rejects_malformed_serialized_specs(spec: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        MutationPlan.from_spec(spec)
