"""Pytest configuration for agent-core tests."""

import subprocess
from datetime import date
from pathlib import Path

import pytest


def run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def ledger_workspace(tmp_path: Path) -> Path:
    """Create a minimal valid ledger workspace with a sidecar."""
    data = tmp_path / "data"
    sidecar = data / "agent_inc"
    sidecar.mkdir(parents=True)
    month = date.today().strftime("%Y-%m")

    (data / "main.beancount").write_text(
        'option "title" "Test Ledger"\n'
        'option "operating_currency" "CNY"\n'
        'include "agent_inc/main.beancount"\n'
    )
    (sidecar / "main.beancount").write_text(
        "2020-01-01 open Assets:Cash CNY\n"
        "2020-01-01 open Assets:Bank:Checking CNY\n"
        "2020-01-01 open Expenses:Food:Dining CNY\n"
        "2020-01-01 open Expenses:Transport:Gas CNY\n"
        "2020-01-01 open Income:Salary CNY\n"
        "2020-01-01 open Equity:Opening-Balances CNY\n"
        f'include "{month}.beancount"\n'
        "\n"
        '2020-01-01 * "Opening balance"\n'
        "  Assets:Cash                  10000 CNY\n"
        "  Assets:Bank:Checking          5000 CNY\n"
        "  Expenses:Food:Dining             0 CNY\n"
        "  Expenses:Transport:Gas           0 CNY\n"
        "  Income:Salary                     0 CNY\n"
        "  Equity:Opening-Balances      -15000 CNY\n"
    )
    (sidecar / f"{month}.beancount").write_text(
        '2026-05-12 * "Lunch"\n  Expenses:Food:Dining  85 CNY\n  Assets:Cash          -85 CNY\n'
    )
    return tmp_path


@pytest.fixture
def bare_ledger_repo(tmp_path: Path, ledger_workspace: Path) -> Path:
    """Create a bare remote containing the minimal ledger."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    run_git(["init", "--bare", str(remote)], tmp_path)
    run_git(["clone", str(remote), str(seed)], tmp_path)
    run_git(["config", "user.email", "test@example.com"], seed)
    run_git(["config", "user.name", "Test"], seed)

    subprocess.run(
        ["cp", "-R", str(ledger_workspace / "data"), str(seed / "data")],
        check=True,
        capture_output=True,
    )
    run_git(["add", "data"], seed)
    run_git(["commit", "-m", "seed ledger"], seed)
    run_git(["push", "origin", "HEAD"], seed)
    return remote
