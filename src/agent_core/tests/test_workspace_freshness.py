"""Behavioral checks for bounded snapshots and asynchronous refresh publication."""

from threading import Event
from unittest.mock import Mock

from agent_core.services.workspace import CachedWorkspaceManager, LocalGitService


def manager_for(tmp_path, bare_ledger_repo, monkeypatch, freshness=30):
    git = LocalGitService(str(bare_ledger_repo))
    manager = CachedWorkspaceManager(git, ttl_seconds=-1, read_freshness_seconds=freshness)
    monkeypatch.setattr(manager, "CACHE_ROOT", str(tmp_path / "cache"))
    manager.acquire("owner", "ignored")
    return manager


def test_recent_snapshot_does_not_wait_for_background_network(
    tmp_path, bare_ledger_repo, monkeypatch
):
    manager = manager_for(tmp_path, bare_ledger_repo, monkeypatch)
    entered, release = Event(), Event()
    original = manager._refresh

    def slow_refresh(*args):
        entered.set()
        assert release.wait(5)
        original(*args)

    monkeypatch.setattr(manager, "_refresh", slow_refresh)
    try:
        manager.acquire_and_copy(
            "owner", "ignored", None, str(tmp_path / "first"), read_recent=True
        )
        assert entered.wait(2)
        manager.acquire_and_copy(
            "owner", "ignored", None, str(tmp_path / "second"), read_recent=True
        )
        assert (tmp_path / "second" / "data" / "main.beancount").exists()
    finally:
        release.set()
        manager.close()


def test_deadline_not_extended_by_snapshot_reads(tmp_path, bare_ledger_repo, monkeypatch):
    manager = manager_for(tmp_path, bare_ledger_repo, monkeypatch)
    key = manager._cache_key("owner", str(bare_ledger_repo))
    observed = manager._state(key)["observed_at"]
    monkeypatch.setattr(manager, "_schedule_refresh", lambda *args: None)
    refresh = Mock(wraps=manager._refresh)
    monkeypatch.setattr(manager, "_refresh", refresh)
    monkeypatch.setattr("agent_core.services.workspace.time.time", lambda: observed + 20)
    manager.acquire_and_copy("owner", "ignored", None, str(tmp_path / "recent"), read_recent=True)
    assert manager._state(key)["observed_at"] == observed
    refresh.assert_not_called()
    monkeypatch.setattr("agent_core.services.workspace.time.time", lambda: observed + 31)
    manager.acquire_and_copy("owner", "ignored", None, str(tmp_path / "expired"), read_recent=True)
    refresh.assert_called_once()
    manager.close()


def test_invalidation_discards_old_background_candidate(tmp_path, bare_ledger_repo, monkeypatch):
    manager = manager_for(tmp_path, bare_ledger_repo, monkeypatch)
    key = manager._cache_key("owner", str(bare_ledger_repo))
    entered, release = Event(), Event()
    original = manager._refresh

    def slow_refresh(*args):
        entered.set()
        assert release.wait(5)
        original(*args)

    monkeypatch.setattr(manager, "_refresh", slow_refresh)
    try:
        manager.acquire_and_copy(
            "owner", "ignored", None, str(tmp_path / "snapshot"), read_recent=True
        )
        assert entered.wait(2)
        manager.invalidate("owner", "ignored")
        generation = manager._state(key)["generation"]
    finally:
        release.set()
        manager.close()
    assert manager._state(key) == {"generation": generation, "observed_at": None}


def test_write_acquisition_never_reuses_recent_refresh(tmp_path, bare_ledger_repo, monkeypatch):
    manager = manager_for(tmp_path, bare_ledger_repo, monkeypatch)
    refresh = Mock(wraps=manager._refresh)
    monkeypatch.setattr(manager, "_refresh", refresh)
    manager.acquire_and_copy("owner", "ignored", None, str(tmp_path / "confirm"))
    refresh.assert_called_once()
    manager.close()


def test_disabled_freshness_always_refreshes(tmp_path, bare_ledger_repo, monkeypatch):
    manager = manager_for(tmp_path, bare_ledger_repo, monkeypatch, freshness=0)
    refresh = Mock(wraps=manager._refresh)
    monkeypatch.setattr(manager, "_refresh", refresh)
    manager.acquire_and_copy(
        "owner", "ignored", None, str(tmp_path / "read"), read_recent=True, admitted_at=0
    )
    refresh.assert_called_once()
    manager.close()


def test_overlapping_read_shares_observation_without_refresh(
    tmp_path, bare_ledger_repo, monkeypatch
):
    manager = manager_for(tmp_path, bare_ledger_repo, monkeypatch)
    key = manager._cache_key("owner", str(bare_ledger_repo))
    observed = manager._state(key)["observed_at"]
    monkeypatch.setattr("agent_core.services.workspace.time.time", lambda: observed + 31)
    monkeypatch.setattr(manager, "_schedule_refresh", lambda *args: None)
    refresh = Mock(wraps=manager._refresh)
    monkeypatch.setattr(manager, "_refresh", refresh)
    try:
        manager.acquire_and_copy(
            "owner",
            "ignored",
            None,
            str(tmp_path / "overlap"),
            read_recent=True,
            admitted_at=observed - 1,
        )
        refresh.assert_not_called()
    finally:
        manager.close()


def test_failed_required_refresh_invalidates_readiness(tmp_path, bare_ledger_repo, monkeypatch):
    import pytest

    manager = manager_for(tmp_path, bare_ledger_repo, monkeypatch)
    key = manager._cache_key("owner", str(bare_ledger_repo))
    monkeypatch.setattr(manager, "_refresh", Mock(side_effect=RuntimeError("fixture")))
    try:
        with pytest.raises(RuntimeError):
            manager.acquire_and_copy("owner", "ignored", None, str(tmp_path / "strict"))
        assert manager._state(key)["observed_at"] is None
    finally:
        manager.close()


def test_background_failure_does_not_extend_deadline(tmp_path, bare_ledger_repo, monkeypatch):
    manager = manager_for(tmp_path, bare_ledger_repo, monkeypatch)
    key = manager._cache_key("owner", str(bare_ledger_repo))
    before = manager._state(key)
    monkeypatch.setattr(manager, "_refresh", Mock(side_effect=RuntimeError("fixture")))
    try:
        manager.acquire_and_copy("owner", "ignored", None, str(tmp_path / "read"), read_recent=True)
        manager.close()
        assert manager._state(key) == before
    finally:
        manager.close()


def test_background_publish_never_changes_existing_snapshot(
    tmp_path, bare_ledger_repo, monkeypatch
):
    from pathlib import Path

    manager = manager_for(tmp_path, bare_ledger_repo, monkeypatch)
    entered, release = Event(), Event()

    def changed_refresh(candidate, *args):
        entered.set()
        assert release.wait(5)
        (Path(candidate) / "fixture_marker").write_text("new generation")

    monkeypatch.setattr(manager, "_refresh", changed_refresh)
    snapshot = tmp_path / "snapshot"
    try:
        manager.acquire_and_copy("owner", "ignored", None, str(snapshot), read_recent=True)
        assert entered.wait(2)
        assert not (snapshot / "fixture_marker").exists()
    finally:
        release.set()
        manager.close()
    assert not (snapshot / "fixture_marker").exists()
    key = manager._cache_key("owner", str(bare_ledger_repo))
    assert (Path(manager._cache_path(key)) / "fixture_marker").exists()


def test_candidate_allocation_failure_releases_capacity(tmp_path, bare_ledger_repo, monkeypatch):
    manager = manager_for(tmp_path, bare_ledger_repo, monkeypatch)
    monkeypatch.setattr(
        "agent_core.services.workspace.tempfile.mkdtemp", Mock(side_effect=OSError("fixture"))
    )
    assert manager._background_capacity.acquire(blocking=False)
    key = manager._cache_key("owner", str(bare_ledger_repo))
    try:
        manager._refresh_candidate(key, "ignored", None, None)
        assert manager._background_capacity.acquire(blocking=False)
        manager._background_capacity.release()
    finally:
        manager.close()
