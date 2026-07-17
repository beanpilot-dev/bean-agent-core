import ast
import json
from pathlib import Path

from agent_core.services import WorkflowToolDependencies
from agent_core.services.types import (
    FileReadResult,
    PriceResult,
    QueryResult,
    SandboxResult,
    ToolCompleted,
)
from agent_core.workflow.tools import (
    MODEL_TOOLS,
    tool_account_balance,
    tool_fetch_price,
    tool_find_transactions,
    tool_ingest_file,
    tool_ledger_calculate_balance_adjustment,
    tool_ledger_commit_transaction,
    tool_ledger_import_transactions,
    tool_ledger_open_account,
    tool_ledger_prepare_balance_reconciliation,
    tool_ledger_prepare_balance_update,
    tool_ledger_prepare_change_set,
    tool_ledger_update_transaction,
    tool_query,
    tool_run_python,
)


class FakeQueries:
    def get_balance(self, workspace, account, as_of_date=None, ledger_config=None):
        return QueryResult(status="SUCCESS", account=account, balance="42 CNY")

    def find_transactions(
        self,
        workspace,
        account=None,
        date_from=None,
        date_to=None,
        narration_contains=None,
        limit=20,
        ledger_config=None,
    ):
        return QueryResult(status="SUCCESS", count=1, rows=[{"account": account}])

    def query_bql(self, workspace, bql, ledger_config=None):
        return QueryResult(status="SUCCESS", bql=bql)


class FakeIngestion:
    def read_file(self, file_path):
        return FileReadResult(status="SUCCESS", file_path=file_path, content="date,amount")

    def run_python(self, code, input_files=None, stage=False, stage_label="import"):
        return SandboxResult(status="SUCCESS", stdout=f"ran:{stage_label}", exit_code=0)


class FakePrices:
    def fetch_price(self, symbol):
        return PriceResult(status="SUCCESS", symbol=symbol, price=123, currency="CNY")


class FakeMutations:
    @staticmethod
    def _completed(tool_name, **result):
        return ToolCompleted(tool_name=tool_name, result=result)

    def prepare_commit(
        self,
        workspace,
        transaction_text,
        commit_message,
        whitelist=None,
        ledger_config=None,
    ):
        return self._completed(
            "ledger_commit_transaction", workspace=workspace, message=commit_message
        )

    def prepare_update(
        self,
        workspace,
        target_date,
        narration,
        new_transaction_text,
        commit_message,
        whitelist=None,
        ledger_config=None,
    ):
        return self._completed("ledger_update_transaction", target_date=target_date)

    def prepare_open(
        self,
        workspace,
        account_name,
        currency,
        open_date,
        display_name=None,
        ledger_config=None,
    ):
        return self._completed("ledger_open_account", account_name=account_name)

    def prepare_bulk(
        self,
        workspace,
        transactions_text="",
        commit_message="",
        transactions_file=None,
        whitelist=None,
        ledger_config=None,
    ):
        return self._completed("ledger_import_transactions", transactions_file=transactions_file)

    def prepare_change_set(
        self,
        workspace,
        operations,
        commit_message,
        whitelist=None,
        ledger_config=None,
    ):
        return self._completed("ledger_prepare_change_set", operation_count=len(operations))

    def calculate_balance_adjustment(
        self,
        workspace,
        observed_date,
        account,
        amount,
        currency,
        cutoff="end_of_day",
        ledger_config=None,
    ):
        return self._completed("ledger_calculate_balance_adjustment", cutoff=cutoff)

    def prepare_balance_reconciliation(
        self,
        workspace,
        observed_date,
        account,
        amount,
        currency,
        adjustment_account,
        cutoff="end_of_day",
        commit_message="",
        ledger_config=None,
    ):
        return self._completed(
            "ledger_prepare_balance_reconciliation",
            adjustment_account=adjustment_account,
        )

    def prepare_balance_update(
        self,
        workspace,
        assertion_date,
        account,
        currency,
        adjustment_account,
        commit_message="",
        ledger_config=None,
    ):
        return self._completed("ledger_prepare_balance_update", assertion_date=assertion_date)


def _config() -> dict:
    return {
        "configurable": {
            "workspace": "/isolated/request",
            "tool_dependencies": WorkflowToolDependencies(
                queries=FakeQueries(),
                ingestion=FakeIngestion(),
                prices=FakePrices(),
                mutations=FakeMutations(),
            ),
        }
    }


def test_workflow_tools_use_injected_fake_ports() -> None:
    config = _config()

    balance = json.loads(tool_account_balance.func("Assets:Cash", config=config))
    price = json.loads(tool_fetch_price.func("USD/CNY", config=config))
    file_result = json.loads(tool_ingest_file.func("/tmp/upload.csv", config=config))
    sandbox = json.loads(tool_run_python.func("print('ok')", config=config))
    mutation = json.loads(
        tool_ledger_commit_transaction.func("txn", "message", config=config)
    )

    assert balance["balance"] == "42 CNY"
    assert price["price"] == 123
    assert file_result["content"] == "date,amount"
    assert sandbox["stdout"] == "ran:import"
    assert mutation["result"]["workspace"] == "/isolated/request"


def test_every_migrated_tool_is_wired_to_its_port() -> None:
    config = _config()
    results = [
        json.loads(tool_find_transactions.func("Assets:Cash", config=config)),
        json.loads(tool_query.func("SELECT account", config=config)),
        json.loads(
            tool_ledger_update_transaction.func(
                "2026-07-01", "Lunch", "txn", "update", config=config
            )
        ),
        json.loads(
            tool_ledger_import_transactions.func(
                "", "bulk", "/tmp/staged.beancount", config=config
            )
        ),
        json.loads(
            tool_ledger_open_account.func(
                "Assets:Bank", "CNY", "2026-07-01", "Bank", config=config
            )
        ),
        json.loads(
            tool_ledger_prepare_change_set.func(
                [{"type": "commit_transaction"}], "change set", config=config
            )
        ),
        json.loads(
            tool_ledger_calculate_balance_adjustment.func(
                "2026-07-01", "Assets:Cash", "10", "CNY", config=config
            )
        ),
        json.loads(
            tool_ledger_prepare_balance_reconciliation.func(
                "2026-07-01",
                "Assets:Cash",
                "10",
                "CNY",
                "Equity:Opening-Balances",
                config=config,
            )
        ),
        json.loads(
            tool_ledger_prepare_balance_update.func(
                "2026-07-01",
                "Assets:Cash",
                "CNY",
                "Equity:Opening-Balances",
                config=config,
            )
        ),
    ]

    assert all(result["status"] in {"SUCCESS", "completed"} for result in results)
    assert results[0]["rows"] == [{"account": "Assets:Cash"}]
    assert results[2]["result"]["target_date"] == "2026-07-01"
    assert results[3]["result"]["transactions_file"] == "/tmp/staged.beancount"
    assert results[5]["result"]["operation_count"] == 1


def test_injected_dependencies_are_hidden_from_model_schemas() -> None:
    for workflow_tool in [tool_fetch_price, tool_ingest_file, tool_run_python]:
        assert "config" not in workflow_tool.args_schema.model_json_schema()["properties"]


def test_model_tool_descriptions_stay_compact() -> None:
    assert sum(len(tool.description or "") for tool in MODEL_TOOLS) <= 5_000


def test_removed_query_surfaces_are_not_model_visible() -> None:
    names = {tool.name for tool in MODEL_TOOLS}
    assert names.isdisjoint({"ledger_query_report", "ledger_query_template"})


def test_workflow_layer_does_not_import_legacy_ledger_or_create_service_globals() -> None:
    workflow_dir = Path(__file__).parents[1] / "workflow"
    forbidden_import = "agent_core.ledger"
    forbidden_globals = {"_ledger", "_queries", "_gateway", "_prices", "_ingestion"}

    for source_path in workflow_dir.glob("*.py"):
        tree = ast.parse(source_path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != forbidden_import, source_path
            if isinstance(node, ast.Import):
                assert all(alias.name != forbidden_import for alias in node.names), source_path
        assigned_globals = {
            target.id
            for node in tree.body
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            if isinstance(target, ast.Name)
        }
        assert assigned_globals.isdisjoint(forbidden_globals), source_path
