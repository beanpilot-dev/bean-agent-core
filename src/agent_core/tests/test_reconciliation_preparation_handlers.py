"""Focused parity coverage for reconciliation calculation and preparation."""

from datetime import date
from decimal import Decimal
from pathlib import Path

from agent_core.services.mutations.coordinator import MutationCoordinator
from agent_core.services.mutations.facts import semantic_facts_hold
from agent_core.services.mutations.handlers.balance_reconciliation import (
    BalanceReconciliationPreparationHandler,
)
from agent_core.services.mutations.handlers.balance_update import (
    BalanceUpdatePreparationHandler,
)
from agent_core.services.mutations.handlers.contracts import PreparedMutation
from agent_core.services.reconciliation import ReconciliationCalculator
from agent_core.services.types import InvariantViolation, LedgerConfig, QueryResult


def _add_checkpoint(workspace: Path, assertion_date: str = "2026-06-01") -> None:
    target = workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
    target.write_text(
        target.read_text()
        + f"\n{assertion_date} balance Assets:Bank:Checking  5000 CNY\n"
    )


def _custom_workspace(tmp_path: Path) -> tuple[Path, LedgerConfig]:
    sidecar = tmp_path / "books" / "agent_sidecar"
    sidecar.mkdir(parents=True)
    month = date.today().strftime("%Y-%m")
    config = LedgerConfig(
        entry_path="books/root.beancount",
        sidecar_main_path="books/agent_sidecar/main.beancount",
        sidecar_write_dir="books/agent_sidecar",
    )
    (tmp_path / "books" / "root.beancount").write_text(
        'option "operating_currency" "CNY"\n'
        'include "agent_sidecar/main.beancount"\n'
    )
    (sidecar / "main.beancount").write_text(
        "2020-01-01 open Assets:Bank:Checking CNY\n"
        "2020-01-01 open Equity:Opening-Balances CNY\n"
        f'include "{month}.beancount"\n'
    )
    (sidecar / f"{month}.beancount").write_text(
        '2020-01-01 * "Opening balance"\n'
        "  Assets:Bank:Checking       5000 CNY\n"
        "  Equity:Opening-Balances   -5000 CNY\n"
    )
    return tmp_path, config


def test_calculator_preserves_end_and_start_of_day_cutoffs(
    ledger_workspace: Path,
) -> None:
    calculator = ReconciliationCalculator()

    end = calculator.calculate_balance_adjustment(
        str(ledger_workspace),
        "2026-05-31",
        "Assets:Bank:Checking",
        "5120",
        "CNY",
    )
    start = calculator.calculate_balance_adjustment(
        str(ledger_workspace),
        "2026-06-01",
        "Assets:Bank:Checking",
        "5000",
        "CNY",
        "start_of_day",
    )

    assert isinstance(end, QueryResult)
    assert end.as_of == "2026-06-01"
    assert end.balance == "5000 CNY"
    assert end.rows == [
        {
            "observed_date": "2026-05-31",
            "cutoff": "end_of_day",
            "assertion_date": "2026-06-01",
            "ledger_balance": "5000 CNY",
            "observed_balance": "5120 CNY",
            "unexplained_difference": "120 CNY",
        }
    ]
    assert isinstance(start, QueryResult)
    assert start.as_of == "2026-06-01"
    assert start.rows[0]["assertion_date"] == "2026-06-01"


def test_calculator_finds_existing_checkpoint_in_active_include_graph(
    ledger_workspace: Path,
) -> None:
    _add_checkpoint(ledger_workspace)

    checkpoint = ReconciliationCalculator().existing_balance_assertion(
        str(ledger_workspace), "2026-06-01", "Assets:Bank:Checking", "CNY"
    )

    assert checkpoint == Decimal("5000")


def test_reconciliation_handler_owns_payload_plan_and_semantic_inputs(
    ledger_workspace: Path,
) -> None:
    prepared = BalanceReconciliationPreparationHandler().build(
        str(ledger_workspace),
        observed_date="2026-05-31",
        account="Assets:Bank:Checking",
        amount="5120.00",
        currency="CNY",
        adjustment_account="Equity:Opening-Balances",
        cutoff="end_of_day",
        commit_message="",
    )

    assert isinstance(prepared, PreparedMutation)
    assert prepared.handler_key == "balance_reconciliation"
    assert prepared.action_type == "balance_reconciliation"
    assert prepared.plan.commit_message == "chore(ledger): reconcile balance"
    assert prepared.execution_spec == {
        "observed_date": "2026-05-31",
        "cutoff": "end_of_day",
        "account": "Assets:Bank:Checking",
        "amount": "5120.00",
        "currency": "CNY",
        "adjustment_account": "Equity:Opening-Balances",
        "is_checkpoint_update": False,
        "commit_message": "chore(ledger): reconcile balance",
    }
    generated = str(prepared.preview_fields["generated_text"])
    assert "Assets:Bank:Checking  120 CNY" in generated
    assert "Equity:Opening-Balances  -120 CNY" in generated
    assert "2026-06-01 balance Assets:Bank:Checking  5120 CNY" in generated
    assert prepared.display_fields["diff"] == generated
    assert prepared.embed_preview_in_display is False
    assert prepared.validation_preview_fields == ("target_file",)
    fact_kinds = {fact.kind for fact in prepared.plan.semantic_facts}
    assert fact_kinds == {"account_state", "balance_state", "checkpoint_state"}
    account_subjects = {
        fact.subject
        for fact in prepared.plan.semantic_facts
        if fact.kind == "account_state"
    }
    assert account_subjects == {
        "Assets:Bank:Checking",
        "Equity:Opening-Balances",
    }


def test_reconciliation_handler_preserves_zero_adjustment_text(
    ledger_workspace: Path,
) -> None:
    prepared = BalanceReconciliationPreparationHandler().build(
        str(ledger_workspace),
        observed_date="2026-05-31",
        account="Assets:Bank:Checking",
        amount="5000",
        currency="CNY",
        adjustment_account="Equity:Opening-Balances",
    )

    assert isinstance(prepared, PreparedMutation)
    assert prepared.preview_fields["adjustment"] == "0 CNY"
    assert "Assets:Bank:Checking  0 CNY" in prepared.plan.operations[0].text
    assert "Equity:Opening-Balances  0 CNY" in prepared.plan.operations[0].text
    assert MutationCoordinator().validate(
        str(ledger_workspace), prepared.plan
    ).check_output == ""


def test_normal_reconciliation_rejects_an_existing_checkpoint(
    ledger_workspace: Path,
) -> None:
    _add_checkpoint(ledger_workspace)

    result = BalanceReconciliationPreparationHandler().build(
        str(ledger_workspace),
        observed_date="2026-05-31",
        account="Assets:Bank:Checking",
        amount="5000",
        currency="CNY",
        adjustment_account="Equity:Opening-Balances",
    )

    assert isinstance(result, InvariantViolation)
    assert result.invariant == "RECONCILIATION_CHECKPOINT_EXISTS"


def test_balance_update_handler_preserves_shared_action_type_and_repair_payload(
    ledger_workspace: Path,
) -> None:
    _add_checkpoint(ledger_workspace)

    prepared = BalanceUpdatePreparationHandler().build(
        str(ledger_workspace),
        assertion_date="2026-06-01",
        account="Assets:Bank:Checking",
        currency="CNY",
        adjustment_account="Equity:Opening-Balances",
        commit_message="",
    )

    assert isinstance(prepared, PreparedMutation)
    assert prepared.handler_key == "balance_update"
    assert prepared.action_type == "balance_reconciliation"
    assert prepared.execution_spec == {
        "observed_date": "2026-05-31",
        "cutoff": "end_of_day",
        "account": "Assets:Bank:Checking",
        "amount": "5000",
        "currency": "CNY",
        "adjustment_account": "Equity:Opening-Balances",
        "is_checkpoint_update": True,
        "commit_message": "chore(ledger): update balance checkpoint",
    }
    assert prepared.display_fields["title"] == "Balance checkpoint update"
    assert "does not rewrite" in str(prepared.display_fields["warning"])
    assert " balance Assets:Bank:Checking " not in prepared.plan.operations[0].text
    assert {fact.kind for fact in prepared.plan.semantic_facts} == {
        "account_state",
        "balance_state",
        "checkpoint_state",
    }


def test_handlers_use_custom_ledger_layout(tmp_path: Path) -> None:
    workspace, config = _custom_workspace(tmp_path)

    calculated = ReconciliationCalculator().calculate_balance_adjustment(
        str(workspace),
        "2026-05-31",
        "Assets:Bank:Checking",
        "5120",
        "CNY",
        ledger_config=config,
    )
    prepared = BalanceReconciliationPreparationHandler().build(
        str(workspace),
        config,
        observed_date="2026-05-31",
        account="Assets:Bank:Checking",
        amount="5120",
        currency="CNY",
        adjustment_account="Equity:Opening-Balances",
    )

    assert isinstance(calculated, QueryResult)
    assert calculated.balance == "5000 CNY"
    assert isinstance(prepared, PreparedMutation)
    assert semantic_facts_hold(
        str(workspace), prepared.plan.semantic_facts, config
    )
