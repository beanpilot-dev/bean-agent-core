"""Compatibility tests for the runtime-neutral tool gateway."""

from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_core.services.ledger import Beancount, _digest_payload
from agent_core.services.tool_gateway import ToolExecutionGateway
from agent_core.services.types import (
    ApprovalProof,
    IntegrityFailed,
    QueryResult,
    ToolApprovalRequired,
    ToolCompleted,
    ToolRepairableError,
)
from agent_core.workflow.tools import MODEL_TOOLS

TXN = '2026-06-15 * "Dinner"\n  Expenses:Food:Dining  100 CNY\n  Assets:Cash          -100 CNY'


@pytest.fixture
def git_service() -> Mock:
    service = Mock()
    service.commit_and_push.return_value = {
        "ok": True,
        "error": None,
        "push": "PUSHED: ok",
    }
    return service


def test_fake_host_receives_approval_required_and_applies_after_proof(
    ledger_workspace: Path,
    git_service: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Beancount, "bean_format", lambda *_args: None)
    gateway = ToolExecutionGateway()

    outcome = gateway.prepare_commit(str(ledger_workspace), TXN, "record dinner")

    assert isinstance(outcome, ToolApprovalRequired)
    assert outcome.status == "approval_required"
    assert outcome.pending_action["status"] == "PENDING_ACTION"
    assert outcome.pending_action["execution_spec"]["transaction_text"] == TXN
    assert "messages" not in outcome.pending_action
    assert "sse" not in outcome.pending_action

    host_persisted_payload = outcome.pending_action.copy()
    proof = ApprovalProof(
        approved_by="user_123",
        approved_at="2026-07-02T00:00:00Z",
        approval_id="approval_123",
        pending_action_id=str(outcome.pending_action["pending_action_id"]),
        payload_digest=_digest_payload(outcome.pending_action),
        integrity_digest=str(outcome.pending_action["digest"]),
        host="fake-mcp-host",
    )

    applied = gateway.apply_approved_action(
        workspace=str(ledger_workspace),
        pending_action=host_persisted_payload,
        approval_proof=proof,
        repo_url="repo",
        git_service=git_service,
    )

    assert isinstance(applied, ToolCompleted)
    assert applied.status == "completed"
    assert applied.result["status"] == "APPLIED"
    target = ledger_workspace / "data" / "agent_inc" / f"{date.today():%Y-%m}.beancount"
    assert "Dinner" in target.read_text()


def test_gateway_rejects_apply_without_approval_proof(
    ledger_workspace: Path,
    git_service: Mock,
) -> None:
    gateway = ToolExecutionGateway()
    outcome = gateway.prepare_commit(str(ledger_workspace), TXN, "record dinner")
    assert isinstance(outcome, ToolApprovalRequired)

    applied = gateway.apply_approved_action(
        workspace=str(ledger_workspace),
        pending_action=outcome.pending_action,
        approval_proof={"approved_by": "", "approved_at": "", "approval_id": ""},
        repo_url="repo",
        git_service=git_service,
    )

    assert isinstance(applied, IntegrityFailed)
    assert applied.status == "INTEGRITY_FAILED"
    git_service.commit_and_push.assert_not_called()


def test_gateway_rejects_apply_with_unbound_approval_proof(
    ledger_workspace: Path,
    git_service: Mock,
) -> None:
    gateway = ToolExecutionGateway()
    outcome = gateway.prepare_commit(str(ledger_workspace), TXN, "record dinner")
    assert isinstance(outcome, ToolApprovalRequired)

    applied = gateway.apply_approved_action(
        workspace=str(ledger_workspace),
        pending_action=outcome.pending_action,
        approval_proof={
            "approved_by": "user_123",
            "approved_at": "2026-07-02T00:00:00Z",
            "approval_id": "approval_123",
            "pending_action_id": "pa_other",
            "payload_digest": _digest_payload(outcome.pending_action),
            "integrity_digest": outcome.pending_action["digest"],
        },
        repo_url="repo",
        git_service=git_service,
    )

    assert isinstance(applied, IntegrityFailed)
    assert applied.status == "INTEGRITY_FAILED"
    git_service.commit_and_push.assert_not_called()


def test_gateway_maps_validation_failure_to_repairable_error(
    ledger_workspace: Path,
) -> None:
    outcome = ToolExecutionGateway().prepare_commit(
        str(ledger_workspace),
        '2026-06-15 * "Bad"\n  Expenses:Food:Dining  100 CNY',
        "bad",
    )

    assert isinstance(outcome, ToolRepairableError)
    assert outcome.status == "repairable_error"
    assert outcome.error_type == "VALIDATION_FAILED"
    assert outcome.result["status"] == "VALIDATION_FAILED"


def test_gateway_maps_successful_read_result_to_completed() -> None:
    result = ToolExecutionGateway().normalize(
        "ledger_account_balance",
        QueryResult(account="Assets:Cash", balance="10 CNY"),
    )

    assert isinstance(result, ToolCompleted)
    assert result.status == "completed"
    assert result.result["status"] == "SUCCESS"


def test_model_visible_tools_do_not_expose_apply_or_confirm() -> None:
    names = {tool.name for tool in MODEL_TOOLS}

    assert "apply_approved_action" not in names
    assert not any(name.startswith("confirm_") for name in names)
    assert not any(name.startswith("prepare_") for name in names)
