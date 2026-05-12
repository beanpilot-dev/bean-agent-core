"""Tests for agent-core ledger mutations."""

import json
import os
import tempfile

import pytest

from agent_core.ledger import mutations
from agent_core.ledger.state import get_agent_target_file


@pytest.fixture
def workspace():
    """Create a minimal Beancount workspace in a temp directory."""
    with tempfile.TemporaryDirectory() as tmp:
        # Create the sidecar structure
        data_dir = os.path.join(tmp, "data")
        agent_inc = os.path.join(data_dir, "agent_inc")
        os.makedirs(agent_inc)

        # Write minimal main.beancount
        main_beancount = os.path.join(data_dir, "main.beancount")
        with open(main_beancount, "w") as f:
            f.write(
                'option "title" "Test Ledger"\n'
                'option "operating_currency" "CNY"\n'
                'include "agent_inc/main.beancount"\n'
            )

        # Write agent_inc/main.beancount with open directives + seed transaction
        # (bean-query only returns accounts that appear in transaction postings,
        #  not from open directives alone)
        agent_main = os.path.join(agent_inc, "main.beancount")
        with open(agent_main, "w") as f:
            f.write(
                "2020-01-01 open Assets:Cash\n"
                "2020-01-01 open Assets:Bank:Checking\n"
                "2020-01-01 open Expenses:Food:Dining\n"
                "2020-01-01 open Expenses:Transport:Gas\n"
                "2020-01-01 open Income:Salary\n"
                "2020-01-01 open Equity:Opening-Balances\n"
                "\n"
                "; Seed transaction so bean-query discovers all accounts\n"
                "2020-01-01 * \"Opening balance\"\n"
                "  Assets:Cash  10000 CNY\n"
                "  Assets:Bank:Checking  5000 CNY\n"
                "  Expenses:Food:Dining  0 CNY\n"
                "  Expenses:Transport:Gas  0 CNY\n"
                "  Income:Salary  0 CNY\n"
                "  Equity:Opening-Balances  -15000 CNY\n"
            )

        # Create current month target file
        from datetime import datetime
        month = datetime.now().strftime("%Y-%m")
        target = os.path.join(agent_inc, f"{month}.beancount")
        with open(target, "w") as f:
            f.write("")

        yield tmp


class TestValidateAccounts:
    def test_known_accounts_pass(self, workspace):
        result = mutations._validate_accounts(
            workspace,
            '2026-05-12 * "Test"\n  Expenses:Food:Dining  50 CNY\n  Assets:Cash\n',
        )
        assert result is None

    def test_unknown_account_fails(self, workspace):
        result = mutations._validate_accounts(
            workspace,
            '2026-05-12 * "Test"\n  Expenses:Unknown:Thing  50 CNY\n  Assets:Cash\n',
        )
        assert result is not None
        assert result["invariant"] == "ACCOUNT_WHITELIST"
        assert "Expenses:Unknown:Thing" in str(result["provided"])

    def test_conversation_scope_rejects_out_of_scope(self, workspace):
        result = mutations._validate_accounts(
            workspace,
            '2026-05-12 * "Test"\n  Expenses:Transport:Gas  50 CNY\n  Assets:Cash\n',
            whitelist=["Expenses:Food"],
        )
        assert result is not None
        assert result["invariant"] == "CONVERSATION_SCOPE"

    def test_conversation_scope_allows_in_scope(self, workspace):
        result = mutations._validate_accounts(
            workspace,
            '2026-05-12 * "Test"\n  Expenses:Food:Dining  50 CNY\n  Assets:Cash\n',
            whitelist=["Expenses:Food", "Assets:Cash"],
        )
        assert result is None


class TestCommitTransaction:
    def test_preview_returns_json(self, workspace):
        txn = '2026-05-12 * "Lunch"\n  Expenses:Food:Dining  85 CNY\n  Assets:Cash\n'
        result = json.loads(
            mutations.commit_transaction(workspace, txn, "test: lunch")
        )
        assert result["status"] == "PREVIEW"
        assert "Expenses:Food:Dining" in result["accounts_validated"]

    def test_commit_writes_and_passes_bean_check(self, workspace):
        txn = '2026-05-12 * "Lunch"\n  Expenses:Food:Dining  85 CNY\n  Assets:Cash\n'
        result = json.loads(
            mutations.commit_transaction(
                workspace, txn, "test: lunch", confirmed=True
            )
        )
        # Without git, this will fail on git commit, but bean-check should pass
        # (we accept DEPENDENCY_UNAVAILABLE because no git in test)
        assert result["status"] in ("SUCCESS", "DEPENDENCY_UNAVAILABLE")
        if result["status"] == "DEPENDENCY_UNAVAILABLE":
            assert "git" in result.get("error", "").lower()

        # Verify transaction was written to disk
        target = get_agent_target_file(workspace)
        with open(os.path.join(workspace, target)) as f:
            content = f.read()
        assert "Lunch" in content
        assert "85 CNY" in content

    def test_commit_auto_reverts_on_bean_check_failure(self, workspace):
        txn = '2026-05-12 * "Bad"\n  Expenses:Food:Dining  100 CNY\n'  # unbalanced
        result = json.loads(
            mutations.commit_transaction(
                workspace, txn, "test: bad", confirmed=True
            )
        )
        assert result["status"] == "VALIDATION_FAILED"
        assert result["reverted"] is True

        # Verify file was reverted (no "Bad" left)
        target = get_agent_target_file(workspace)
        with open(os.path.join(workspace, target)) as f:
            content = f.read()
        assert "Bad" not in content


class TestOpenAccount:
    def test_preview_validates_name_format(self, workspace):
        result = json.loads(
            mutations.open_account(
                workspace, "assets:cash", None, "2026-01-01"
            )
        )
        assert result["status"] == "INVARIANT_VIOLATION"
        assert result["invariant"] == "ACCOUNT_NAME_FORMAT"

    def test_preview_rejects_duplicate(self, workspace):
        result = json.loads(
            mutations.open_account(
                workspace, "Assets:Cash", None, "2026-01-01"
            )
        )
        assert result["status"] == "INVARIANT_VIOLATION"
        assert result["invariant"] == "ACCOUNT_ALREADY_EXISTS"

    def test_preview_accepts_valid_new_account(self, workspace):
        result = json.loads(
            mutations.open_account(
                workspace, "Assets:Bank:Savings", "CNY", "2026-01-01"
            )
        )
        assert result["status"] == "PREVIEW"
        assert "Assets:Bank:Savings" in result["account"]
