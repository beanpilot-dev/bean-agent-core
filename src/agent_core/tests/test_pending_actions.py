"""Contract tests for runtime-neutral pending-action semantics."""

from agent_core.services.approvals.contracts import (
    PendingActionService as PackagedPendingActionService,
)
from agent_core.services.pending_actions import PendingActionService, digest_payload
from agent_core.services.types import IntegrityFailed, PendingAction


def _payload_without_integrity(payload: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"digest", "signature", "status", "message"}
    }


def _resign(payload: dict[str, object]) -> None:
    digest = digest_payload(_payload_without_integrity(payload))
    payload["digest"] = digest
    payload["signature"] = f"sha256:{digest}"


def test_contract_construction_preserves_schema_digest_and_high_risk_policy() -> None:
    result = PendingActionService.create_pending_action(
        action_type="bulk_commit",
        execution_spec={"transactions_text": "example", "commit_message": "record batch"},
        display={"diff": "example"},
        validation={"transaction_count": 25, "dry_run": {"status": "validated"}},
    )

    assert isinstance(result, PendingAction)
    assert set(result.__dict__) == {
        "status",
        "pending_action_id",
        "action_type",
        "schema_version",
        "execution_spec",
        "display",
        "validation",
        "policy",
        "expires_at",
        "idempotency_key",
        "continue_after_approval",
        "continuation_reason",
        "next_intent_summary",
        "digest",
        "signature",
        "message",
    }
    assert result.policy == {
        "version": "risk-policy-v1",
        "requires_approval": True,
        "risk": "high",
        "reasons": ["bulk_transaction_count"],
        "requires_elevated_review": True,
    }
    assert result.signature == f"sha256:{result.digest}"
    assert result.idempotency_key == digest_payload({
        "action_type": "bulk_commit",
        "execution_spec": result.execution_spec,
        "validation": result.validation,
    })
    assert PendingActionService.verify_pending_action(result.__dict__.copy()) is None


def test_contract_verification_rejects_expired_payload_with_valid_integrity() -> None:
    result = PendingActionService.create_pending_action(
        action_type="commit_transaction",
        execution_spec={"transaction_text": "example", "commit_message": "record"},
        display={"diff": "example"},
        validation={"dry_run": {"status": "validated"}},
    )
    payload = result.__dict__.copy()
    payload["expires_at"] = "2020-01-01T00:00:00+00:00"
    _resign(payload)

    integrity = PendingActionService.verify_pending_action(payload)

    assert isinstance(integrity, IntegrityFailed)
    assert integrity.error == "Pending action has expired."


def test_legacy_pending_action_import_is_the_packaged_contract() -> None:
    assert PendingActionService is PackagedPendingActionService
