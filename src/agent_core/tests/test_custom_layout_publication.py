"""Git publication coverage for runtime-configured sidecar layouts."""

import subprocess
from datetime import date
from pathlib import Path

import pytest

from agent_core.services.mutations import MutationExecutor, MutationPlanner
from agent_core.services.types import LedgerConfig
from agent_core.services.workspace import LocalGitService

TRANSACTION = (
    '2026-06-15 * "Dinner"\n  Expenses:Food:Dining  100 CNY\n  Assets:Cash          -100 CNY'
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class _CleanValidator:
    def check(self, _workspace: str, _config: object = None) -> tuple[bool, str]:
        return True, ""


class _NoopFormatter:
    def format(self, _workspace: str, _path: str) -> None:
        return None


class _RecordingFormatter:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def format(self, _workspace: str, path: str) -> None:
        self.paths.append(path)


def _custom_layout_remote(tmp_path: Path, *, include_current_month: bool = True) -> Path:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    month = f"{date.today():%Y-%m}.beancount"
    _git(["init", "--bare", str(remote)], tmp_path)
    _git(["init", str(seed)], tmp_path)
    _git(["config", "user.email", "test@example.com"], seed)
    _git(["config", "user.name", "Test"], seed)
    sidecar = seed / "books" / "agent_sidecar"
    sidecar.mkdir(parents=True)
    (seed / "books" / "root.beancount").write_text('include "agent_sidecar/main.beancount"\n')
    main_text = (
        "2020-01-01 open Assets:Cash CNY\n"
        "2020-01-01 open Expenses:Food:Dining CNY\n"
    )
    if include_current_month:
        main_text += f'include "{month}"\n'
        (sidecar / month).write_text("; custom sidecar\n")
    (sidecar / "main.beancount").write_text(main_text)
    (seed / "notes.txt").write_text("seed note\n")
    _git(["add", "books", "notes.txt"], seed)
    _git(["commit", "-m", "seed custom ledger"], seed)
    _git(["remote", "add", "origin", str(remote)], seed)
    _git(["push", "origin", "HEAD"], seed)
    return remote


def test_executor_publishes_only_touched_custom_sidecar_paths(tmp_path: Path) -> None:
    remote = _custom_layout_remote(tmp_path)
    workspace = tmp_path / "workspace"
    service = LocalGitService(str(remote))
    service.clone(str(workspace), "ignored")
    config = LedgerConfig(
        entry_path="books/root.beancount",
        sidecar_main_path="books/agent_sidecar/main.beancount",
        sidecar_write_dir="books/agent_sidecar",
    )
    (workspace / "notes.txt").write_text("unrelated dirty note\n")

    touched, git, failure = MutationExecutor(
        ledger_validator=_CleanValidator(), formatter=_NoopFormatter()
    ).apply_and_publish(
        str(workspace),
        MutationPlanner.commit(TRANSACTION, "record dinner"),
        "ignored",
        service,
        config=config,
    )

    month_path = f"books/agent_sidecar/{date.today():%Y-%m}.beancount"
    assert touched == (month_path,)
    assert git["ok"] is True
    assert failure == ""
    assert TRANSACTION in _git(["show", f"HEAD:{month_path}"], workspace).stdout
    assert _git(["show", "HEAD:notes.txt"], workspace).stdout == "seed note\n"
    assert _git(["diff", "--cached", "--name-only"], workspace).stdout == ""
    assert _git(["diff", "--name-only"], workspace).stdout == "notes.txt\n"
    committed_paths = _git(
        ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"], workspace
    ).stdout.splitlines()
    assert committed_paths == [month_path]


def test_executor_publishes_all_changed_paths_when_append_creates_month(
    tmp_path: Path,
) -> None:
    remote = _custom_layout_remote(tmp_path, include_current_month=False)
    workspace = tmp_path / "workspace"
    service = LocalGitService(str(remote))
    service.clone(str(workspace), "ignored")
    config = LedgerConfig(
        entry_path="books/root.beancount",
        sidecar_main_path="books/agent_sidecar/main.beancount",
        sidecar_write_dir="books/agent_sidecar",
    )
    formatter = _RecordingFormatter()
    (workspace / "notes.txt").write_text("unrelated dirty note\n")

    touched, git, failure = MutationExecutor(
        ledger_validator=_CleanValidator(), formatter=formatter
    ).apply_and_publish(
        str(workspace),
        MutationPlanner.commit(TRANSACTION, "record dinner"),
        "ignored",
        service,
        config=config,
    )

    main_path = "books/agent_sidecar/main.beancount"
    month_path = f"books/agent_sidecar/{date.today():%Y-%m}.beancount"
    assert touched == (month_path,)
    assert git["ok"] is True
    assert failure == ""
    assert set(formatter.paths) == {
        str(workspace / main_path),
        str(workspace / month_path),
    }
    assert f'include "{date.today():%Y-%m}.beancount"' in _git(
        ["show", f"HEAD:{main_path}"], workspace
    ).stdout
    assert TRANSACTION in _git(["show", f"HEAD:{month_path}"], workspace).stdout
    assert set(
        _git(
            ["diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
            workspace,
        ).stdout.splitlines()
    ) == {main_path, month_path}
    assert _git(["show", "HEAD:notes.txt"], workspace).stdout == "seed note\n"
    assert _git(["diff", "--cached", "--name-only"], workspace).stdout == ""
    assert _git(["diff", "--name-only"], workspace).stdout == "notes.txt\n"


@pytest.mark.parametrize(
    "paths",
    [[], [""], ["/absolute.beancount"], ["../escape.beancount"], ["."]],
)
def test_git_service_rejects_unsafe_explicit_stage_paths(tmp_path: Path, paths: list[str]) -> None:
    workspace = tmp_path / "workspace"
    _git(["init", str(workspace)], tmp_path)

    with pytest.raises(ValueError):
        LocalGitService(str(workspace)).commit_and_push(
            str(workspace), "unsafe", "ignored", paths=paths
        )
