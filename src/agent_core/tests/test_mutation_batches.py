"""Shared-file approval regressions, including actual Git publication."""

import subprocess
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from agent_core.main import app
from agent_core.services.approvals.contracts import PendingActionService
from agent_core.services.ledger import LedgerService
from agent_core.services.mutations.batches import (
    check_independent_batch,
    coordinate_prepared_actions,
)
from agent_core.services.operations.pending_actions import PendingActionOperationHandler
from agent_core.services.types import ApplyReceipt, IntegrityFailed, InvariantViolation
from agent_core.services.workspace import LocalGitService


def opens(workspace: str) -> list[dict]:
    service = LedgerService()
    return [
        asdict(service.prepare_open(workspace, name, "CNY", "2020-01-01"))
        for name in ["Assets:Test:One", "Assets:Test:Two"]
    ]


def test_shared_opens_publish_together_once(bare_ledger_repo: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "batch-workspace"
    git = LocalGitService(str(bare_ledger_repo))
    git.clone(str(workspace), "ignored")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
    actions = opens(str(workspace))
    assert check_independent_batch(actions) == "BATCH_REPREPARE_REQUIRED"
    coordinated = coordinate_prepared_actions(str(workspace), actions)
    assert len(coordinated) == 1
    composite = coordinated[0]
    assert PendingActionService.verify_pending_action(composite) is None
    assert len(composite["display"]["items"]) == 2
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
        == head
    )
    result = LedgerService().apply_pending_action(str(workspace), composite, "ignored", git)
    assert isinstance(result, ApplyReceipt)
    count = subprocess.check_output(
        ["git", "rev-list", "--count", f"{head}..HEAD"], cwd=workspace, text=True
    ).strip()
    assert count == "1"
    remote_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=bare_ledger_repo, text=True
    ).strip()
    assert remote_head == result.receipt["commit_sha"]
    content = subprocess.check_output(
        ["git", "show", "HEAD:data/agent_inc/main.beancount"], cwd=bare_ledger_repo, text=True
    )
    assert all(f"open {name}" in content for name in ["Assets:Test:One", "Assets:Test:Two"])
    repeated = LedgerService().apply_pending_action(str(workspace), composite, "ignored", git)
    assert isinstance(repeated, InvariantViolation)
    assert repeated.invariant == "MUTATION_PRECONDITION_FAILED"
    assert (
        subprocess.check_output(
            ["git", "rev-list", "--count", f"{head}..HEAD"], cwd=workspace, text=True
        ).strip()
        == "1"
    )


def test_composite_rejects_external_revision_before_publication(ledger_workspace: Path) -> None:
    action = coordinate_prepared_actions(str(ledger_workspace), opens(str(ledger_workspace)))[0]
    sidecar = ledger_workspace / "data/agent_inc/main.beancount"
    sidecar.write_text(sidecar.read_text() + "\n; external revision\n")

    class Publisher:
        def commit_and_push(self, *args):
            pytest.fail("stale plan published")

    result = LedgerService().apply_pending_action(
        str(ledger_workspace), action, "fixture", Publisher()
    )
    assert isinstance(result, InvariantViolation)
    assert result.invariant == "MUTATION_PRECONDITION_FAILED"


def test_combined_invalid_group_never_mutates_workspace(ledger_workspace: Path) -> None:
    path = str(ledger_workspace)
    action = opens(path)[0]
    sidecar = ledger_workspace / "data/agent_inc/main.beancount"
    original = sidecar.read_bytes()
    with pytest.raises(ValueError, match="failed validation"):
        coordinate_prepared_actions(path, [action, action])
    assert sidecar.read_bytes() == original


def test_tampered_or_stale_group_cannot_be_resealed(ledger_workspace: Path) -> None:
    path = str(ledger_workspace)
    actions = opens(path)
    actions[0]["execution_spec"]["mutation_plan"]["commit_message"] = "tampered"
    assert check_independent_batch(actions) == "INTEGRITY_FAILED"
    with pytest.raises(ValueError):
        coordinate_prepared_actions(path, actions)
    actions = opens(path)
    sidecar = ledger_workspace / "data/agent_inc/main.beancount"
    sidecar.write_text(sidecar.read_text() + "\n; changed\n")
    with pytest.raises(ValueError, match="stale"):
        coordinate_prepared_actions(path, actions)


def test_account_closes_preserve_elevated_review(ledger_workspace: Path) -> None:
    sidecar = ledger_workspace / "data/agent_inc/main.beancount"
    sidecar.write_text(
        sidecar.read_text()
        + "\n2020-01-01 open Assets:Test:One CNY\n2020-01-01 open Assets:Test:Two CNY\n"
    )
    path = str(ledger_workspace)
    service = LedgerService()
    actions = [
        asdict(service.prepare_account_close(path, name, "2026-06-01"))
        for name in ["Assets:Test:One", "Assets:Test:Two"]
    ]
    composite = coordinate_prepared_actions(path, actions)[0]
    assert composite["policy"]["requires_elevated_review"] is True
    assert "account_closure" in composite["policy"]["reasons"]
    result = service.apply_pending_action(path, composite, "fixture", object())
    assert isinstance(result, IntegrityFailed)


@pytest.mark.asyncio
async def test_batch_guard_checks_without_publishing(ledger_workspace: Path) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        actions = opens(str(ledger_workspace))
        response = await client.post(
            "/agent/actions/check-batch", json={"pending_actions": actions}
        )
        assert response.status_code == 200
        assert response.json()["error"]["code"] == "BATCH_REPREPARE_REQUIRED"
        assert response.json()["publication_outcome"] == "not_published"
        composite = coordinate_prepared_actions(str(ledger_workspace), actions)
        response = await client.post(
            "/agent/actions/check-batch", json={"pending_actions": composite}
        )
        assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_operation_distinguishes_no_publication_from_publisher_exception(ledger_workspace):
    path = str(ledger_workspace)

    class Lifecycle:
        @contextmanager
        def open(self, **kwargs):
            yield type("Workspace", (), {"path": path})()

    class Publisher:
        def commit_and_push(self, *args):
            raise ValueError("ambiguous publication adapter failure")

    operation = PendingActionOperationHandler(Lifecycle(), Publisher())
    action = coordinate_prepared_actions(path, opens(path))[0]
    unknown = await operation.run(
        repo_url="fixture",
        token=None,
        user_id="fixture",
        request_id="fixture",
        pending_action=action,
    )
    assert unknown["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert "publication_outcome" not in unknown
    sidecar = ledger_workspace / "data/agent_inc/main.beancount"
    sidecar.write_text(sidecar.read_text() + "\n; external revision\n")
    rejected = await operation.run(
        repo_url="fixture",
        token=None,
        user_id="fixture",
        request_id="fixture",
        pending_action=action,
    )
    assert rejected["error"]["code"] == "MUTATION_PRECONDITION_FAILED"
    assert rejected["publication_outcome"] == "not_published"
