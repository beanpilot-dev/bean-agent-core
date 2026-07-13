"""Runtime-neutral pending-action contract construction and verification."""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from .types import IntegrityFailed, LedgerMutationAction, PendingAction

_PENDING_ACTION_SCHEMA_VERSION = 1
_PENDING_ACTION_TTL_MINUTES = 30


def digest_payload(payload: dict[str, object]) -> str:
    """Return the canonical SHA-256 digest used by pending-action contracts."""
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _pending_action_digest_input(action: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in action.items()
        if key not in {"digest", "signature", "status", "message"}
    }


def _classify_action_risk(
    action_type: str, validation: dict[str, object]
) -> dict[str, object]:
    reasons: list[str] = []
    risk = "normal"
    txn_count = validation.get("transaction_count")
    if (
        action_type in {"bulk_commit", "change_set"}
        and isinstance(txn_count, int)
        and txn_count >= 25
    ):
        risk = "high"
        reasons.append("bulk_transaction_count")
    if action_type == "update_transaction":
        risk = "elevated"
        reasons.append("historical_update")
    if action_type == "balance_reconciliation":
        risk = "elevated"
        reasons.append("balance_reconciliation")
    return {
        "version": "risk-policy-v1",
        "risk": risk,
        "reasons": reasons,
        "requires_elevated_review": risk == "high",
    }


class PendingActionService:
    """Build and verify immutable approval contracts without runtime dependencies."""

    @staticmethod
    def create_pending_action(
        *,
        action_type: str,
        execution_spec: dict[str, object],
        display: dict[str, object],
        validation: dict[str, object],
    ) -> PendingAction:
        mutation = LedgerMutationAction(
            action_type=action_type,
            schema_version=_PENDING_ACTION_SCHEMA_VERSION,
            execution_spec=execution_spec,
            display=display,
            validation=validation,
        )
        pending_action_id = f"pa_{uuid.uuid4().hex[:16]}"
        expires_at = (
            datetime.now(timezone.utc) + timedelta(minutes=_PENDING_ACTION_TTL_MINUTES)
        ).isoformat()
        idempotency_key = digest_payload({
            "action_type": mutation.action_type,
            "execution_spec": mutation.execution_spec,
            "validation": mutation.validation,
        })
        payload = {
            "pending_action_id": pending_action_id,
            "action_type": mutation.action_type,
            "schema_version": mutation.schema_version,
            "execution_spec": mutation.execution_spec,
            "display": mutation.display,
            "validation": mutation.validation,
            "policy": {
                "version": "pending-action-v1",
                "requires_approval": True,
                **_classify_action_risk(mutation.action_type, mutation.validation),
            },
            "expires_at": expires_at,
            "idempotency_key": idempotency_key,
            "continue_after_approval": False,
            "continuation_reason": "",
            "next_intent_summary": "",
        }
        digest = digest_payload(payload)
        return PendingAction(
            pending_action_id=pending_action_id,
            action_type=action_type,
            schema_version=_PENDING_ACTION_SCHEMA_VERSION,
            execution_spec=execution_spec,
            display=display,
            validation=validation,
            policy=payload["policy"],
            expires_at=expires_at,
            idempotency_key=idempotency_key,
            continue_after_approval=False,
            continuation_reason="",
            next_intent_summary="",
            digest=digest,
            signature=f"sha256:{digest}",
            message="Prepared action is awaiting explicit user approval.",
        )

    @staticmethod
    def verify_pending_action(action: dict[str, object]) -> IntegrityFailed | None:
        pending_action_id = str(action.get("pending_action_id") or "")
        digest = action.get("digest")
        signature = action.get("signature")
        if not isinstance(digest, str) or not digest:
            return IntegrityFailed(
                pending_action_id=pending_action_id,
                error="Missing pending action digest.",
            )
        expected = digest_payload(_pending_action_digest_input(action))
        if digest != expected or signature != f"sha256:{expected}":
            return IntegrityFailed(
                pending_action_id=pending_action_id,
                error="Pending action integrity check failed.",
            )
        expires_at = action.get("expires_at")
        if isinstance(expires_at, str) and expires_at:
            try:
                expires = datetime.fromisoformat(expires_at)
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires <= datetime.now(timezone.utc):
                    return IntegrityFailed(
                        pending_action_id=pending_action_id,
                        error="Pending action has expired.",
                    )
            except ValueError:
                return IntegrityFailed(
                    pending_action_id=pending_action_id,
                    error="Pending action expiry is invalid.",
                )
        return None
