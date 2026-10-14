"""Bounded offloading for blocking agent-core operations."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from threading import Lock
from typing import TypeVar
from weakref import WeakKeyDictionary

from ..timing import stage_timer

T = TypeVar("T")

# Keep both active workers and submitted work bounded. Callers wait on the
# semaphore before submitting, so the executor never accumulates an unbounded
# queue of ledger operations.
_WORKER_LIMIT = 4
_executor = ThreadPoolExecutor(max_workers=_WORKER_LIMIT, thread_name_prefix="agent-ledger")
_capacities: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()
_capacity_lock = Lock()


async def run_blocking(function, /, *args, **kwargs) -> T:
    """Run one blocking operation with request context and safe cancellation.

    Once submitted, the operation owns its workspace until it returns. A
    cancelled caller waits for that return before releasing capacity or
    allowing its surrounding lifecycle to clean the workspace.
    """

    loop = asyncio.get_running_loop()
    with _capacity_lock:
        capacity = _capacities.setdefault(loop, asyncio.Semaphore(_WORKER_LIMIT))
    with stage_timer("worker_wait"):
        await capacity.acquire()
    context = copy_context()
    call = partial(function, *args, **kwargs)
    future = loop.run_in_executor(_executor, context.run, call)
    cancelled = False
    try:
        while not future.done():
            try:
                await asyncio.wait({future})
            except asyncio.CancelledError:
                # asyncio.wait does not cancel the executor future. Keep
                # ownership through repeated cancellation until it completes.
                cancelled = True
        if cancelled:
            # Retrieve a worker error even when cancellation wins the result.
            try:
                future.exception()
            except asyncio.CancelledError:
                pass
            raise asyncio.CancelledError
        return future.result()
    finally:
        capacity.release()
