from unittest.mock import AsyncMock

import httpx
import pytest

from agent_core.main import (
    ApplyPendingActionRequest,
    RepoInfo,
    _digest_object,
    _orchestrator,
    _verify_apply_request,
    app,
)


def _request(payload: dict, **overrides) -> ApplyPendingActionRequest:
    values = {
        "repo": RepoInfo(url="repo", token="token"),
        "user_id": "user_123",
        "pending_action_id": payload["pending_action_id"],
        "payload_digest": _digest_object(payload),
        "integrity_digest": payload["digest"],
        "opaque_payload": payload,
    }
    values.update(overrides)
    return ApplyPendingActionRequest(**values)


def test_apply_request_verifies_top_level_digests() -> None:
    payload = {
        "pending_action_id": "pa_123",
        "action_type": "commit_transaction",
        "execution_spec": {"transaction_text": "redacted in test"},
        "digest": "embedded_digest",
        "signature": "sha256:embedded_digest",
    }

    assert _verify_apply_request(_request(payload), payload) is None


def test_apply_request_digest_matches_srv_stable_json_for_non_ascii_payload() -> None:
    payload = {
        "pending_action_id": "pa_123",
        "action_type": "commit_transaction",
        "execution_spec": {
            "transaction_text": '2026-07-03 * "缴纳话费" "pab支付"',
            "commit_message": "记录话费",
        },
        "digest": "embedded_digest",
        "signature": "sha256:embedded_digest",
    }
    srv_style_digest = "ab6d250387ee187bff1f5da72a506705de98658fe2c0d768e6cc1508b0c4b2e3"

    assert _digest_object(payload) == srv_style_digest
    assert _verify_apply_request(_request(payload), payload) is None


def test_apply_request_rejects_mismatched_pending_action_id() -> None:
    payload = {
        "pending_action_id": "pa_123",
        "action_type": "commit_transaction",
        "digest": "embedded_digest",
        "signature": "sha256:embedded_digest",
    }

    response = _verify_apply_request(
        _request(payload, pending_action_id="pa_other"),
        payload,
    )

    assert response is not None
    assert response.status_code == 409


def test_apply_request_rejects_missing_embedded_pending_action_id() -> None:
    payload = {
        "action_type": "commit_transaction",
        "digest": "embedded_digest",
        "signature": "sha256:embedded_digest",
    }

    req = _request({**payload, "pending_action_id": "pa_123"}, pending_action_id="pa_123")
    response = _verify_apply_request(req, payload)

    assert response is not None
    assert response.status_code == 409


def test_apply_request_rejects_mismatched_payload_digest() -> None:
    payload = {
        "pending_action_id": "pa_123",
        "action_type": "commit_transaction",
        "digest": "embedded_digest",
        "signature": "sha256:embedded_digest",
    }

    response = _verify_apply_request(
        _request(payload, payload_digest="bad_digest"),
        payload,
    )

    assert response is not None
    assert response.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_field",
    ["pending_action_id", "payload_digest", "integrity_digest"],
)
async def test_apply_route_rejects_missing_top_level_binding_before_apply(
    missing_field: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "pending_action_id": "pa_123",
        "action_type": "commit_transaction",
        "digest": "embedded_digest",
        "signature": "sha256:embedded_digest",
    }
    body = {
        "repo": {"url": "repo", "token": "token"},
        "user_id": "user_123",
        "pending_action_id": payload["pending_action_id"],
        "payload_digest": _digest_object(payload),
        "integrity_digest": payload["digest"],
        "opaque_payload": payload,
    }
    body.pop(missing_field)
    apply_mock = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(_orchestrator, "run_apply_pending_action", apply_mock)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/agent/actions/apply", json=body)

    assert response.status_code == 422
    apply_mock.assert_not_awaited()


def test_apply_request_rejects_mismatched_integrity_digest() -> None:
    payload = {
        "pending_action_id": "pa_123",
        "action_type": "commit_transaction",
        "digest": "embedded_digest",
        "signature": "sha256:embedded_digest",
    }

    response = _verify_apply_request(
        _request(payload, integrity_digest="other_digest"),
        payload,
    )

    assert response is not None
    assert response.status_code == 409
