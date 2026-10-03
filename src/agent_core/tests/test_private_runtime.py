"""Focused contract coverage for the deterministic private runtime."""

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_core.services.plugin_policy import (
    RepositoryPluginRejectedError,
    enforce_plugin_policy,
)
from agent_core.services.runtime import (
    PrivateLedgerRuntime,
    RuntimeExecuteRequest,
    RuntimeProtocolError,
    _render_transaction,
)
from agent_core.services.types import LedgerConfig


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_private_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        RuntimeExecuteRequest(
            execution_id="exec_1",
            operation="preflight",
            repo={"url": "https://example.invalid/repo", "token": "token"},
            user_id="user_1",
            branch="main",
            unexpected="nope",
        )


def test_structured_transaction_renderer_rejects_raw_text_escape_hatches() -> None:
    with pytest.raises(RuntimeProtocolError):
        _render_transaction(
            {
                "date": "2026-09-13",
                "flag": "*",
                "narration": "Lunch",
                "postings": [],
                "raw_text": "2026-09-13 * Lunch",
            }
        )


def test_structured_transaction_renderer_identifies_malformed_units_path() -> None:
    with pytest.raises(RuntimeProtocolError) as captured:
        _render_transaction(
            {
                "date": "2026-09-13",
                "narration": "Lunch",
                "postings": [
                    {
                        "account": "Expenses:Food:Dining",
                        "units": {"number": "not-decimal", "currency": "CNY"},
                    },
                    {
                        "account": "Assets:Cash",
                        "units": {"number": "-12.50", "currency": "CNY"},
                    },
                ],
            },
            "operations[2].transaction",
        )

    assert captured.value.code == "INVALID_ARGUMENTS"
    assert captured.value.details["path"] == "operations[2].transaction.postings[0].units.number"
    assert "Do not retry the unchanged payload" in captured.value.details["remediation"]


def test_unsupported_structured_operation_has_stable_actionable_error(
    ledger_workspace: Path,
) -> None:
    runtime = PrivateLedgerRuntime(object())
    with pytest.raises(RuntimeProtocolError) as captured:
        runtime._build_operation(
            {"op": "add_transaction", "date": "2026-09-13", "postings": []},
            str(ledger_workspace),
            LedgerConfig(),
            None,
        )

    assert captured.value.code == "INVALID_ARGUMENTS"
    assert captured.value.details["path"] == "operations[0]"

    with pytest.raises(RuntimeProtocolError) as unsupported:
        runtime._build_operation(
            {"kind": "add_transaction"},
            str(ledger_workspace),
            LedgerConfig(),
            None,
        )

    assert unsupported.value.code == "UNSUPPORTED_CHANGE"
    assert unsupported.value.details["path"] == "operations[0].kind"
    assert "kind=create_transaction" in unsupported.value.details["remediation"]


def test_structured_transaction_renderer_outputs_bounded_beancount() -> None:
    rendered = _render_transaction(
        {
            "date": "2026-09-13",
            "flag": "*",
            "payee": "Cafe",
            "narration": "Lunch",
            "postings": [
                {
                    "account": "Expenses:Food:Dining",
                    "units": {"number": "12.50", "currency": "CNY"},
                },
                {"account": "Assets:Cash", "units": {"number": "-12.50", "currency": "CNY"}},
            ],
            "tags": ["mcp"],
            "links": [],
        }
    )
    assert rendered.startswith('2026-09-13 * "Cafe" "Lunch" #mcp')
    assert "Expenses:Food:Dining  12.50 CNY" in rendered
    assert "Assets:Cash  -12.50 CNY" in rendered


def test_plugin_policy_rejects_before_parser(tmp_path: Path) -> None:
    ledger = tmp_path / "data"
    ledger.mkdir()
    (ledger / "main.beancount").write_text(
        'plugin "untrusted.module"\noption "title" "x"\n', encoding="utf-8"
    )
    with pytest.raises(RepositoryPluginRejectedError):
        enforce_plugin_policy(str(tmp_path), LedgerConfig())


def test_plugin_policy_scans_non_beancount_includes(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "main.beancount").write_text('include "included.bean"\n', encoding="utf-8")
    (data / "included.bean").write_text('plugin "untrusted.module"\n', encoding="utf-8")
    with pytest.raises(RepositoryPluginRejectedError):
        enforce_plugin_policy(str(tmp_path), LedgerConfig())


def test_private_runtime_has_no_model_dependency(ledger_workspace: Path) -> None:
    """The runtime can be constructed without PersonalFinanceAgent or API keys."""

    class Lifecycle:
        pass

    runtime = PrivateLedgerRuntime(Lifecycle())
    assert runtime._preparation is not None
    assert runtime._registry.keys()


def test_report_kind_is_allowlisted(ledger_workspace: Path) -> None:
    runtime = PrivateLedgerRuntime(object())
    with pytest.raises(RuntimeProtocolError, match="allowlisted"):
        runtime._report(
            {"report_kind": "arbitrary_bql", "limit": 10},
            str(ledger_workspace),
            LedgerConfig(),
        )


@pytest.mark.parametrize(
    "report_kind",
    ["account_balance", "trial_balance", "income_statement", "cash_flow", "account_activity"],
)
def test_all_report_kinds_are_bounded_and_snapshot_shaped(
    ledger_workspace: Path, report_kind: str
) -> None:
    runtime = PrivateLedgerRuntime(object())
    result = runtime._report(
        {"report_kind": report_kind, "limit": 1},
        str(ledger_workspace),
        LedgerConfig(),
    )
    assert result["report_kind"] == report_kind
    assert result["count"] <= 1
    assert result["total"] >= result["count"]


def test_prepare_change_set_returns_sealed_action_without_writing(ledger_workspace: Path) -> None:
    _git(["init"], ledger_workspace)
    _git(["config", "user.email", "test@example.com"], ledger_workspace)
    _git(["config", "user.name", "Test"], ledger_workspace)
    _git(["add", "data"], ledger_workspace)
    _git(["commit", "-m", "seed"], ledger_workspace)
    month_file = next((ledger_workspace / "data" / "agent_inc").glob("20*.beancount"))
    before = month_file.read_text()

    runtime = PrivateLedgerRuntime(object())
    result = runtime._prepare(
        {
            "commit_message": "Record lunch",
            "operations": [
                {
                    "kind": "create_transaction",
                    "transaction": {
                        "date": "2026-05-13",
                        "narration": "Lunch",
                        "postings": [
                            {
                                "account": "Expenses:Food:Dining",
                                "units": {"number": "10", "currency": "CNY"},
                            },
                            {
                                "account": "Assets:Cash",
                                "units": {"number": "-10", "currency": "CNY"},
                            },
                        ],
                    },
                }
            ],
        },
        str(ledger_workspace),
        LedgerConfig(),
    )
    assert result["status"] == "approval_required"
    assert result["action"]["execution_spec"]["mutation_plan"]["preconditions"]
    assert result["review"]["validation"]["status"] == "validated"
    assert month_file.read_text() == before


def _dependent_operations() -> list[dict]:
    return [
        {
            "kind": "open_account",
            "account_name": "Expenses:Audit",
            "currency": "CNY",
            "open_date": "2026-05-13",
        },
        {
            "kind": "create_transaction",
            "transaction": {
                "date": "2026-05-13",
                "narration": "Dependency test",
                "postings": [
                    {"account": "Expenses:Audit", "units": {"number": "10", "currency": "CNY"}},
                    {"account": "Assets:Cash", "units": {"number": "-10", "currency": "CNY"}},
                ],
            },
        },
    ]


def _initialize_runtime_repo(workspace: Path) -> None:
    _git(["init"], workspace)
    _git(["config", "user.email", "test@example.com"], workspace)
    _git(["config", "user.name", "Test"], workspace)
    _git(["add", "data"], workspace)
    _git(["commit", "-m", "seed"], workspace)


def test_ordered_account_dependency_seals_original_state_and_replays(
    ledger_workspace: Path,
) -> None:
    from agent_core.services.beancount import Beancount
    from agent_core.services.mutations.applier import MutationApplier
    from agent_core.services.mutations.coordinator import MutationCoordinator
    from agent_core.services.mutations.plans import MutationPlan

    _initialize_runtime_repo(ledger_workspace)
    before = {p: p.read_bytes() for p in (ledger_workspace / "data").rglob("*.beancount")}
    result = PrivateLedgerRuntime(object())._prepare(
        {"operations": _dependent_operations()},
        str(ledger_workspace),
        LedgerConfig(),
    )
    assert result["status"] == "approval_required"
    assert {p: p.read_bytes() for p in before} == before
    plan = MutationPlan.from_spec(result["action"]["execution_spec"]["mutation_plan"])
    assert MutationCoordinator._preconditions_hold(str(ledger_workspace), plan, LedgerConfig())
    MutationApplier().apply(str(ledger_workspace), plan, LedgerConfig())
    assert Beancount.bean_check(str(ledger_workspace), LedgerConfig())[0]


@pytest.mark.parametrize("restriction", ["reversed", "whitelist", "closed"])
def test_ordered_dependency_preserves_policy_and_source_isolation(
    ledger_workspace: Path, restriction: str
) -> None:
    _initialize_runtime_repo(ledger_workspace)
    operations = _dependent_operations()
    args: dict = {"operations": operations}
    if restriction == "reversed":
        operations.reverse()
        expected = "ACCOUNT_WHITELIST"
    elif restriction == "whitelist":
        args["whitelist"] = ["Assets:Cash"]
        expected = "CONVERSATION_SCOPE"
    else:
        operations.insert(
            1,
            {"kind": "close_account", "account_name": "Expenses:Audit", "close_date": "2026-05-13"},
        )
        expected = None  # Beancount must reject a posting after close.
        operations[2]["transaction"]["date"] = "2026-05-14"
    before = {p: p.read_bytes() for p in (ledger_workspace / "data").rglob("*.beancount")}
    if expected:
        with pytest.raises(RuntimeProtocolError) as captured:
            PrivateLedgerRuntime(object())._prepare(args, str(ledger_workspace), LedgerConfig())
        assert captured.value.code == expected
    else:
        result = PrivateLedgerRuntime(object())._prepare(
            args, str(ledger_workspace), LedgerConfig()
        )
        assert result["status"] == "error"
        assert result["error"]["code"] == "VALIDATION_FAILED"
    assert {p: p.read_bytes() for p in before} == before
    assert set((ledger_workspace / "data").rglob("*.beancount")) == set(before)


@pytest.mark.parametrize("earlier", ["delete", "duplicate_delete", "update", "create"])
def test_original_transaction_bindings_survive_internal_replay(
    ledger_workspace: Path, earlier: str
) -> None:
    from agent_core.services.beancount import Beancount
    from agent_core.services.mutations.applier import MutationApplier
    from agent_core.services.mutations.coordinator import MutationCoordinator
    from agent_core.services.mutations.plans import MutationPlan
    from agent_core.services.transaction_index import TransactionIndex

    month = next((ledger_workspace / "data" / "agent_inc").glob("20*.beancount"))
    first_text = month.read_text()
    second_text = (
        first_text
        if earlier == "duplicate_delete"
        else first_text.replace("2026-05-12", "2026-05-13").replace('"Lunch"', '"Second lunch"')
    )
    month.write_text(first_text + "\n" + second_text)
    _initialize_runtime_repo(ledger_workspace)
    index = TransactionIndex.build(str(ledger_workspace), LedgerConfig())
    targets = [item for item in index.transactions if item.relative_path.endswith(month.name)]
    first, second = targets
    delete_second = {
        "kind": "delete_transaction",
        "transaction_ref": second.transaction_ref,
        "revision_fingerprint": second.revision_fingerprint,
        "reason": "Synthetic regression",
    }
    if earlier in {"delete", "duplicate_delete"}:
        initial = {
            **delete_second,
            "transaction_ref": first.transaction_ref,
            "revision_fingerprint": first.revision_fingerprint,
        }
    else:
        transaction = {
            "date": "2026-05-11",
            "payee": "Regression",
            "narration": "Earlier",
            "postings": [
                {"account": "Expenses:Food:Dining", "units": {"number": "85", "currency": "CNY"}},
                {"account": "Assets:Cash", "units": {"number": "-85", "currency": "CNY"}},
            ],
        }
        if earlier == "update":
            transaction["tags"] = ["regression"]
            transaction["postings"].extend(
                [
                    {
                        "account": "Equity:Opening-Balances",
                        "units": {"number": "0", "currency": "CNY"},
                    },
                    {"account": "Income:Salary", "units": {"number": "0", "currency": "CNY"}},
                ]
            )
            initial = {
                "kind": "update_transaction",
                "transaction_ref": first.transaction_ref,
                "revision_fingerprint": first.revision_fingerprint,
                "transaction": transaction,
            }
        else:
            initial = {"kind": "create_transaction", "transaction": transaction}
    before = month.read_bytes()
    result = PrivateLedgerRuntime(object())._prepare(
        {"operations": [initial, delete_second]}, str(ledger_workspace), LedgerConfig()
    )
    assert result["status"] == "approval_required"
    assert month.read_bytes() == before
    plan = MutationPlan.from_spec(result["action"]["execution_spec"]["mutation_plan"])
    assert MutationCoordinator._preconditions_hold(str(ledger_workspace), plan, LedgerConfig())
    MutationApplier().apply(str(ledger_workspace), plan, LedgerConfig())
    assert Beancount.bean_check(str(ledger_workspace), LedgerConfig())[0]
    after = TransactionIndex.build(str(ledger_workspace), LedgerConfig())
    remaining = [item for item in after.transactions if item.relative_path.endswith(month.name)]
    assert len(remaining) == (0 if "delete" in earlier else 1 if earlier == "update" else 2)
