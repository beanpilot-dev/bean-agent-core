"""Characterization tests for canonical Git publication identity and recovery."""

from __future__ import annotations

import pytest

from agent_core.services.mutations.publisher import (
    MutationCommitMarker,
    PublicationReconciliation,
    PublishReceipt,
    PublishRequest,
    classify_reconciled_receipt,
)


def test_canonical_commit_marker_carries_action_and_plan_identity() -> None:
    marker = MutationCommitMarker("pending-123", "sha256:sealed-plan")

    trailers = marker.to_commit_trailers()

    assert trailers == (
        "Beanpilot-Pending-Action: pending-123\n"
        "Beanpilot-Mutation-Plan: sha256:sealed-plan"
    )
    assert MutationCommitMarker.from_commit_trailers(f"record transaction\n\n{trailers}") == marker


def test_malformed_or_partial_marker_fails_closed() -> None:
    with pytest.raises(ValueError, match="malformed"):
        MutationCommitMarker.from_commit_trailers("Beanpilot-Pending-Action: pending-123")
    with pytest.raises(ValueError, match="single-line"):
        MutationCommitMarker("pending-123\nother", "digest")


def test_publish_request_carries_remote_cas_value_and_result_tree() -> None:
    request = PublishRequest(
        marker=MutationCommitMarker("pending-123", "sha256:sealed-plan"),
        base_commit="base-sha",
        expected_remote_head="remote-head-sha",
        result_tree="result-tree-sha",
        branch="refs/heads/main",
        commit_message="record dinner",
    )

    assert request.expected_remote_head == "remote-head-sha"
    assert request.result_tree == "result-tree-sha"
    assert request.marker.plan_digest == "sha256:sealed-plan"


def test_publish_receipt_is_complete_reconciliation_evidence() -> None:
    receipt = PublishReceipt(
        action_id="pending-123",
        plan_digest="sha256:sealed-plan",
        base_commit="base-sha",
        published_commit="published-sha",
        result_tree="result-tree-sha",
        branch="refs/heads/main",
    )

    assert receipt.marker == MutationCommitMarker("pending-123", "sha256:sealed-plan")
    assert PublicationReconciliation("published", receipt).receipt == receipt
    assert PublicationReconciliation("not_found").receipt is None
    with pytest.raises(ValueError, match="requires a publish receipt"):
        PublicationReconciliation("published")


def test_reconciliation_rejects_a_reused_action_id_with_another_plan_digest() -> None:
    request = PublishRequest(
        marker=MutationCommitMarker("pending-123", "sha256:expected"),
        base_commit="base-sha",
        expected_remote_head="remote-head-sha",
        result_tree="result-tree-sha",
        branch="refs/heads/main",
        commit_message="record dinner",
    )
    prior = PublishReceipt(
        action_id="pending-123",
        plan_digest="sha256:other-plan",
        base_commit="base-sha",
        published_commit="published-sha",
        result_tree="result-tree-sha",
        branch="refs/heads/main",
    )

    assert classify_reconciled_receipt(request, prior).status == "integrity_failure"
