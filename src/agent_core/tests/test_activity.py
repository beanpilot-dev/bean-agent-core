import asyncio
from uuid import uuid4

from agent_core.services.activity import ActivityCallbackHandler, ActivityEmitter


def test_activity_emitter_builds_ordered_safe_chunks() -> None:
    emitter = ActivityEmitter(run_id="run_123")

    chunk = emitter.emit(
        category="git",
        state="started",
        phase="sync",
        actor="orchestrator",
        visibility="details",
        display_key="agent.git.sync_started",
        display_args={
            "duration_ms": 12.3456,
            "repo_url": "https://example.invalid/private.git",
            "token": "ghs_secret",
            "safe_count": 2,
        },
        fallback_text=" Preparing workspace\n",
        safe_detail_summary="metadata only",
    )

    assert chunk["type"] == "activity"
    assert chunk["run_id"] == "run_123"
    assert chunk["sequence"] == 1
    assert chunk["visibility"] == "details"
    assert chunk["display_key"] == "agent.git.sync_started"
    assert chunk["display_args"] == {"duration_ms": 12.346}
    assert chunk["fallback_text"] == "Preparing workspace"
    assert chunk["safe_detail_summary"] == "metadata only"


def test_activity_emitter_downgrades_invalid_visibility() -> None:
    emitter = ActivityEmitter(run_id="run_123")

    chunk = emitter.emit(
        category="run",
        state="started",
        phase="dispatch",
        actor="orchestrator",
        visibility="unsafe",  # type: ignore[arg-type]
    )

    assert chunk["visibility"] == "internal"


def test_activity_emitter_drops_sensitive_free_text() -> None:
    emitter = ActivityEmitter(run_id="run_123")

    chunk = emitter.emit(
        category="run",
        state="failed",
        phase="dispatch",
        actor="orchestrator",
        visibility="timeline",
        fallback_text='Traceback File "/tmp/private/main.beancount"',
        safe_detail_summary="ledger/main.beancount",
        display_args={
            "safe": "ok",
            "note": "git clone https://example.invalid/private.git",
            "relative": "./books/main.beancount",
        },
    )

    assert chunk["fallback_text"] is None
    assert chunk["safe_detail_summary"] is None
    assert chunk["display_args"] == {"safe": "ok"}


def test_activity_callback_emits_tool_events_without_payloads() -> None:
    emitter = ActivityEmitter(run_id="run_123")
    queue: asyncio.Queue[dict] = asyncio.Queue()
    callback = ActivityCallbackHandler(emitter, queue)
    run_id = uuid4()

    callback.on_tool_start(
        {"name": "ledger_commit"},
        '{"account":"Expenses:Food","amount":"25 USD"}',
        run_id=run_id,
    )
    callback.on_tool_end("raw ledger output", run_id=run_id)

    started = queue.get_nowait()
    completed = queue.get_nowait()
    assert started["category"] == "tool"
    assert started["state"] == "started"
    assert started["actor"] == "bookkeeper"
    assert started["task_id"] == "tool_ledger_commit"
    assert "Expenses:Food" not in str(started)
    assert completed["state"] == "completed"
    assert "raw ledger output" not in str(completed)


def test_activity_emitter_allows_only_safe_numeric_display_args() -> None:
    emitter = ActivityEmitter(run_id="run_123")

    chunk = emitter.emit(
        category="planning",
        state="completed",
        phase="planning",
        actor="planner",
        display_args={
            "task_count": 2,
            "tool_count": 3,
            "duration_ms": 1.2345,
            "balance": 1234.56,
            "total": 25,
            "nested": {"total": 10, "count": 1},
        },
    )

    assert chunk["display_args"] == {
        "task_count": 2,
        "tool_count": 3,
        "duration_ms": 1.234,
        "nested": {"count": 1},
    }
