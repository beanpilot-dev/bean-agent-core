import asyncio
import gc
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient

from agent_core.services.operations.blocking import run_blocking
from agent_core.services.operations.ledger_reads import LedgerReadOperationHandler
from agent_core.services.operations.lifecycle import RequestWorkspaceLifecycle
from agent_core.services.operations.pending_actions import PendingActionOperationHandler
from agent_core.services.workspace import CachedWorkspaceManager


@pytest.mark.asyncio
async def test_delayed_ledger_read_keeps_health_probe_responsive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.main import app
    from agent_core.services.operations import ledger_reads

    worker_started = threading.Event()

    @contextmanager
    def delayed_workspace(**_kwargs):
        worker_started.set()
        time.sleep(1.0)
        yield type("Prepared", (), {"path": "workspace", "setup_ready": True})()

    monkeypatch.setattr(
        ledger_reads.PreflightService,
        "list_accounts",
        lambda *_args: ["Assets:Cash"],
    )
    monkeypatch.setattr(
        ledger_reads.PreflightService,
        "get_raw_open_directives",
        lambda *_args: [],
    )
    lifecycle = type("Lifecycle", (), {"open": staticmethod(delayed_workspace)})()
    handler = LedgerReadOperationHandler(lifecycle)
    read = asyncio.create_task(
        handler.run_accounts(
            repo_url="repo",
            token="token",
            user_id="user",
            request_id="request",
        )
    )
    assert await asyncio.to_thread(worker_started.wait, 1)
    assert not read.done(), "the delayed ledger operation should still overlap /health"
    started = time.monotonic()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    health_elapsed = time.monotonic() - started
    result = await read

    assert result["status"] == "ok"
    assert response.status_code == 200
    assert health_elapsed < 0.25


@pytest.mark.asyncio
async def test_blocking_work_receives_request_context() -> None:
    request_id: ContextVar[str] = ContextVar("request_id")
    token = request_id.set("request-a")
    try:
        assert await run_blocking(request_id.get) == "request-a"
    finally:
        request_id.reset(token)


@pytest.mark.asyncio
async def test_concurrent_workers_keep_request_context_and_arguments_isolated() -> None:
    from agent_core.context import agent_request_id

    def snapshot(path: str, ledger_config: dict) -> tuple[str | None, str, dict]:
        time.sleep(0.02)
        return agent_request_id.get(), path, ledger_config

    async def request(request_id: str, path: str, currency: str):
        token = agent_request_id.set(request_id)
        try:
            return await run_blocking(
                snapshot,
                path,
                {"operating_currency": currency},
            )
        finally:
            agent_request_id.reset(token)

    first, second = await asyncio.gather(
        request("request-a", "/workspace/a", "CNY"),
        request("request-b", "/workspace/b", "USD"),
    )
    assert first == ("request-a", "/workspace/a", {"operating_currency": "CNY"})
    assert second == ("request-b", "/workspace/b", {"operating_currency": "USD"})


@pytest.mark.asyncio
async def test_cancelled_workspace_waits_for_worker_before_cleanup(tmp_path: Path) -> None:
    cache = Mock()
    cache.acquire.return_value = str(tmp_path / "cache")
    git = Mock()
    copy_started = threading.Event()
    allow_copy = threading.Event()
    destroyed_after_copy = []

    def copy(_source: str, _target: str) -> None:
        copy_started.set()
        allow_copy.wait(timeout=2)

    def destroy(path: str) -> None:
        destroyed_after_copy.append(allow_copy.is_set())

    git.copy.side_effect = copy
    git.destroy.side_effect = destroy
    lifecycle = RequestWorkspaceLifecycle(
        cache,
        git,
        workspace_factory=lambda _prefix: str(tmp_path / "request"),
    )

    async def use_workspace() -> None:
        async with lifecycle.open_async(
            repo_url="repo", token="token", user_id="user", prefix="test_"
        ):
            pass

    task = asyncio.create_task(use_workspace())
    assert await asyncio.to_thread(copy_started.wait, 1)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    allow_copy.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert destroyed_after_copy == [True]
    git.destroy.assert_called_once_with(str(tmp_path / "request"))


@pytest.mark.asyncio
async def test_cancelled_cleanup_waits_for_worker_capacity(tmp_path: Path) -> None:
    cache = Mock()
    cache.acquire.return_value = str(tmp_path / "cache")
    git = Mock()
    git.destroy.return_value = None
    lifecycle = RequestWorkspaceLifecycle(
        cache,
        git,
        workspace_factory=lambda _prefix: str(tmp_path / "request"),
    )
    entered = asyncio.Event()
    release_workers = threading.Event()
    all_workers_started = threading.Event()
    workers_started = 0
    workers_lock = threading.Lock()

    def occupy_worker() -> None:
        nonlocal workers_started
        with workers_lock:
            workers_started += 1
            if workers_started == 4:
                all_workers_started.set()
        release_workers.wait(timeout=2)

    async def use_workspace() -> None:
        async with lifecycle.open_async(
            repo_url="repo", token="token", user_id="user", prefix="test_"
        ):
            entered.set()
            await asyncio.Future()

    request = asyncio.create_task(use_workspace())
    await entered.wait()
    workers = [asyncio.create_task(run_blocking(occupy_worker)) for _ in range(4)]
    assert await asyncio.to_thread(all_workers_started.wait, 1)
    request.cancel()
    await asyncio.sleep(0.02)
    request.cancel()
    await asyncio.sleep(0.02)
    assert not request.done()

    release_workers.set()
    await asyncio.gather(*workers)
    with pytest.raises(asyncio.CancelledError):
        await request
    git.destroy.assert_called_once_with(str(tmp_path / "request"))


@pytest.mark.asyncio
async def test_cancelled_approved_apply_finishes_once_and_releases_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    from agent_core.services.operations import pending_actions

    apply_started = threading.Event()
    allow_apply = threading.Event()
    apply_calls = 0
    cleanup_calls = 0

    @contextmanager
    def workspace(**_kwargs):
        nonlocal cleanup_calls
        try:
            yield SimpleNamespace(path="workspace")
        finally:
            cleanup_calls += 1

    def apply(*_args, **_kwargs):
        nonlocal apply_calls
        apply_calls += 1
        apply_started.set()
        allow_apply.wait(timeout=2)
        return SimpleNamespace(status="APPLIED")

    monkeypatch.setattr(pending_actions.LedgerService, "apply_pending_action", apply)
    lifecycle = type("Lifecycle", (), {"open": staticmethod(workspace)})()
    handler = PendingActionOperationHandler(lifecycle, Mock())
    operation = asyncio.create_task(
        handler.run(
            repo_url="repo",
            token="token",
            user_id="user",
            request_id="request",
            pending_action={"sealed": True},
        )
    )
    assert await asyncio.to_thread(apply_started.wait, 1)
    operation.cancel()
    await asyncio.sleep(0.02)
    operation.cancel()
    await asyncio.sleep(0.02)
    assert not operation.done()
    assert cleanup_calls == 0

    allow_apply.set()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert apply_calls == 1
    assert cleanup_calls == 1
    assert await run_blocking(lambda: "capacity returned") == "capacity returned"


@pytest.mark.asyncio
async def test_worker_error_after_cancellation_is_consumed() -> None:
    loop = asyncio.get_running_loop()
    reported = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    worker_started = threading.Event()
    release_worker = threading.Event()

    def fail_after_release() -> None:
        worker_started.set()
        release_worker.wait(timeout=2)
        raise RuntimeError("worker failed after cancellation")

    task = asyncio.create_task(run_blocking(fail_after_release))
    assert await asyncio.to_thread(worker_started.wait, 1)
    task.cancel()
    release_worker.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await run_blocking(lambda: "capacity returned") == "capacity returned"
    del task
    gc.collect()
    await asyncio.sleep(0)
    loop.set_exception_handler(previous_handler)
    assert reported == []


@pytest.mark.asyncio
async def test_worker_capacity_is_bounded() -> None:
    active = 0
    peak = 0
    guard = threading.Lock()

    def occupy() -> None:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with guard:
            active -= 1

    await asyncio.gather(*(run_blocking(occupy) for _ in range(12)))
    assert peak <= 4


def test_cache_refresh_waits_until_same_key_snapshot_copy_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy_started = threading.Event()
    allow_copy = threading.Event()
    second_refresh = threading.Event()
    git = Mock()
    git.effective_repo_url.side_effect = lambda repo, _token: repo

    def copy(_source: str, _destination: str) -> None:
        copy_started.set()
        allow_copy.wait(timeout=2)

    git.copy.side_effect = copy
    manager = CachedWorkspaceManager(git, ttl_seconds=60)
    manager.CACHE_ROOT = str(tmp_path / "cache")
    original_locked_acquire = manager._acquire_locked
    calls = 0

    def record_refresh(key, cache_path, repo_url, token, branch):
        nonlocal calls
        calls += 1
        if calls > 1:
            second_refresh.set()
        original_locked_acquire(key, cache_path, repo_url, token, branch)

    monkeypatch.setattr(manager, "_acquire_locked", record_refresh)
    with ThreadPoolExecutor(max_workers=2) as workers:
        snapshot = workers.submit(
            manager.acquire_and_copy,
            "user",
            "repo",
            "token",
            str(tmp_path / "request"),
        )
        assert copy_started.wait(timeout=1)
        refresh = workers.submit(manager.acquire, "user", "repo", "token")
        assert not second_refresh.wait(timeout=0.05)
        allow_copy.set()
        snapshot.result(timeout=1)
        refresh.result(timeout=1)

    assert second_refresh.is_set()
