import json
from datetime import date
from pathlib import Path

from agent_core.agent import PersonalFinanceAgent
from agent_core.workflow.tools import (
    MODEL_TOOLS,
    tool_ledger_commit_transaction,
    tool_ledger_open_account,
    tool_ledger_prepare_change_set,
)

TXN = '2026-06-15 * "Dinner"\n  Expenses:Food:Dining  100 CNY\n  Assets:Cash          -100 CNY'
DEPENDENT_TXN = (
    '2026-06-16 * "Savings transfer"\n'
    "  Assets:Bank:Savings   100 CNY\n"
    "  Assets:Cash          -100 CNY"
)
PROMPT = Path(__file__).parents[1] / "ledger" / "prompt.md"


def _tool_name(tool) -> str:
    return getattr(tool, "name", "")


def test_model_tool_manifest_excludes_execution_tools() -> None:
    model_names = {_tool_name(tool) for tool in MODEL_TOOLS}

    assert "ledger_commit_transaction" in model_names
    assert "ledger_update_transaction" in model_names
    assert "ledger_import_transactions" in model_names
    assert "ledger_open_account" in model_names
    assert "ledger_prepare_change_set" in model_names
    assert "ledger_preflight" not in model_names
    assert "prepare_commit" not in model_names
    assert "prepare_open" not in model_names
    assert "confirm_commit" not in model_names
    assert "confirm_bulk" not in model_names


def test_default_agent_uses_single_loop_manifest() -> None:
    agent = PersonalFinanceAgent()

    assert agent.model_tools == MODEL_TOOLS
    graph = agent.graph.get_graph()
    node_names = set(graph.nodes)
    assert "agent" in node_names
    assert "tools" in node_names
    assert "planner" not in node_names
    assert "synthesizer" not in node_names


def test_system_prompt_requires_complete_change_sets_before_approval() -> None:
    prompt = " ".join(PROMPT.read_text().split())

    assert "prepare every clear required mutation in the same run" in prompt
    assert "Do not stop after the first obvious mutation" in prompt
    assert "ledger_prepare_change_set" in prompt
    assert "continue_after_approval" in prompt
    assert "next_intent_summary" in prompt


def test_ledger_commit_transaction_returns_approval_required_without_write(
    ledger_workspace: Path,
) -> None:
    target = ledger_workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
    original = target.read_text()

    raw = tool_ledger_commit_transaction.func(
        TXN,
        "record dinner",
        config={"configurable": {"workspace": str(ledger_workspace)}},
    )
    payload = json.loads(raw)

    assert payload["status"] == "approval_required"
    assert payload["action_type"] == "commit_transaction"
    assert payload["policy"]["requires_approval"] is True
    assert payload["pending_action"]["status"] == "PENDING_ACTION"
    assert payload["pending_action"]["execution_spec"]["transaction_text"] == TXN
    assert target.read_text() == original


def test_ledger_open_account_returns_approval_required_without_write(
    ledger_workspace: Path,
) -> None:
    target = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    original = target.read_text()

    raw = tool_ledger_open_account.func(
        "Assets:Bank:Savings",
        "CNY",
        "2026-06-15",
        "Savings",
        config={"configurable": {"workspace": str(ledger_workspace)}},
    )
    payload = json.loads(raw)

    assert payload["status"] == "approval_required"
    assert payload["tool_name"] == "ledger_open_account"
    assert payload["action_type"] == "open_account"
    assert payload["pending_action"]["status"] == "PENDING_ACTION"
    assert payload["pending_action"]["execution_spec"]["account_name"] == "Assets:Bank:Savings"
    assert payload["display"]["diff"].startswith("2026-06-15 open Assets:Bank:Savings")
    assert target.read_text() == original


def test_ledger_prepare_change_set_returns_approval_required_without_write(
    ledger_workspace: Path,
) -> None:
    sidecar_main = ledger_workspace / "data" / "agent_inc" / "main.beancount"
    month_file = ledger_workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
    original_main = sidecar_main.read_text()
    original_month = month_file.read_text()

    raw = tool_ledger_prepare_change_set.func(
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
        config={"configurable": {"workspace": str(ledger_workspace)}},
    )
    payload = json.loads(raw)

    assert payload["status"] == "approval_required"
    assert payload["tool_name"] == "ledger_prepare_change_set"
    assert payload["action_type"] == "change_set"
    assert payload["pending_action"]["status"] == "PENDING_ACTION"
    assert (
        payload["pending_action"]["execution_spec"]["commit_message"]
        == "record savings transfer"
    )
    assert len(payload["display"]["items"]) == 2
    assert sidecar_main.read_text() == original_main
    assert month_file.read_text() == original_month


def test_ledger_commit_transaction_returns_repairable_validation_failure(
    ledger_workspace: Path,
) -> None:
    target = ledger_workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
    original = target.read_text()

    raw = tool_ledger_commit_transaction.func(
        '2026-06-15 * "Bad"\n  Expenses:Food:Dining  100 CNY',
        "bad",
        config={"configurable": {"workspace": str(ledger_workspace)}},
    )
    payload = json.loads(raw)

    assert payload["status"] == "repairable_error"
    assert payload["error_type"] == "VALIDATION_FAILED"
    assert payload["result"]["error"] == "transaction_not_balanced"
    assert payload["result"]["advisory"]["retryable"] is True
    assert target.read_text() == original
