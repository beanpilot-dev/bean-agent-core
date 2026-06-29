"""Structured activity events for the agent-core SSE stream.

Activity chunks are product-semantic status updates. They must stay safe for
SaaS persistence and optional user display, so callers should pass only stable
display keys and metadata values that are not ledger content.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from langchain_core.callbacks.base import BaseCallbackHandler

ActivityVisibility = Literal["internal", "timeline", "details"]

_VALID_VISIBILITY = {"internal", "timeline", "details"}
_SENSITIVE_ARG_KEYS = {
    "api_key",
    "token",
    "secret",
    "password",
    "repo_url",
    "url",
    "path",
    "command",
    "stdout",
    "stderr",
    "prompt",
    "ledger",
    "account",
    "amount",
    "balance",
    "total",
    "value",
    "usage",
}
_ALLOWED_NUMERIC_ARG_KEYS = {"duration_ms", "task_count", "tool_count", "sequence", "count"}
_SENSITIVE_TEXT_PATTERNS = [
    re.compile(
        r"\b(?:sk|ghs|ghp|github_pat|bearer|token|password|secret|api[_-]?key)"
        r"[A-Za-z0-9_:/+=.-]*",
        re.I,
    ),
    re.compile(r"(?:^|\s)(?:/[\w.-]+){2,}"),
    re.compile(r"(?:^|\s)(?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8}"),
    re.compile(r"[A-Za-z]:\\"),
    re.compile(r"\b(?:git|bean-check|bean-query|python|curl|npm|docker)\s+[-\w./:=]+", re.I),
    re.compile(r"\b(?:Assets|Liabilities|Equity|Income|Expenses):[A-Za-z0-9:_-]+"),
    re.compile(r"\b\d+(?:\.\d+)?\s+[A-Z]{2,6}\b"),
    re.compile(r"\bTraceback\b|\bFile \""),
    re.compile(r"https?://|git@"),
]
_NODE_ACTORS = {
    "agent": "agent",
    "tools": "agent",
}


@dataclass
class ActivityEmitter:
    """Build ordered activity SSE chunks for one agent run."""

    run_id: str
    sequence: int = 0
    _seen_keys: set[str] = field(default_factory=set)

    def emit(
        self,
        *,
        category: str,
        state: str,
        phase: str,
        actor: str,
        visibility: ActivityVisibility = "internal",
        display_key: str | None = None,
        display_args: dict[str, Any] | None = None,
        fallback_text: str | None = None,
        safe_detail_summary: str | None = None,
        task_id: str | None = None,
        event_key: str | None = None,
    ) -> dict[str, Any]:
        """Return the next activity chunk.

        event_key lets callers make semantic event ids readable. Sequence is
        still included in the id to avoid collisions when a phase repeats.
        """

        self.sequence += 1
        safe_visibility = visibility if visibility in _VALID_VISIBILITY else "internal"
        default_display_key = f"agent.activity.{_safe_token(phase)}.{_safe_token(state)}"
        key = event_key or f"{phase}.{category}.{state}"
        event_id = self._unique_event_id(key)
        chunk: dict[str, Any] = {
            "type": "activity",
            "event_id": event_id,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "category": _safe_token(category),
            "state": _safe_token(state),
            "phase": _safe_token(phase),
            "actor": _safe_token(actor),
            "visibility": safe_visibility,
            "display_key": display_key or default_display_key,
            "display_args": _sanitize_args(display_args or {}),
            "fallback_text": _safe_text(fallback_text),
            "safe_detail_summary": _safe_text(safe_detail_summary),
        }
        if task_id:
            chunk["task_id"] = _safe_token(task_id)
        return chunk

    def _unique_event_id(self, key: str) -> str:
        base = _safe_token(key).replace(".", "_")
        event_id = f"{base}_{self.sequence}"
        while event_id in self._seen_keys:
            self.sequence += 1
            event_id = f"{base}_{self.sequence}"
        self._seen_keys.add(event_id)
        return event_id


def _safe_token(value: str | None) -> str:
    if not value:
        return "unknown"
    allowed = []
    for char in str(value).lower():
        if char.isalnum() or char in ("_", "-", "."):
            allowed.append(char)
        else:
            allowed.append("_")
    token = "".join(allowed).strip("._-")
    return (token or "unknown")[:80]


def _safe_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if any(pattern.search(text) for pattern in _SENSITIVE_TEXT_PATTERNS):
        return None
    return text[:240]


def _sanitize_args(args: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in args.items():
        normalized_key = str(key).lower()
        if any(part in normalized_key for part in _SENSITIVE_ARG_KEYS):
            continue
        safe_key = _safe_token(str(key))
        safe_value = _sanitize_value(safe_key, value)
        if safe_value is not None:
            safe[safe_key] = safe_value
    return safe


def _sanitize_value(key: str, value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if key in _ALLOWED_NUMERIC_ARG_KEYS else None
    if isinstance(value, float):
        if key not in _ALLOWED_NUMERIC_ARG_KEYS:
            return None
        return round(value, 3)
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list):
        return [
            item
            for item in (_sanitize_value(key, item) for item in value[:10])
            if item is not None
        ]
    if isinstance(value, dict):
        return _sanitize_args(value)
    return _safe_text(type(value).__name__)


class ActivityCallbackHandler(BaseCallbackHandler):
    """LangChain callback bridge that streams safe node/tool milestones."""

    def __init__(self, emitter: ActivityEmitter, queue: asyncio.Queue[dict[str, Any]]):
        self._emitter = emitter
        self._queue = queue
        self._run_context: dict[UUID, dict[str, str]] = {}

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        name = self._safe_name(serialized, kwargs)
        if name not in _NODE_ACTORS:
            return
        actor = _NODE_ACTORS[name]
        self._run_context[run_id] = {"name": name, "actor": actor, "kind": "node"}
        self._put(self._emitter.emit(
            category="node",
            state="started",
            phase="execution",
            actor=actor,
            visibility="details",
            display_key=f"agent.node.{name}.started",
            fallback_text="Workflow step started",
            task_id=f"node_{name}",
            event_key=f"node.{name}.started",
        ))

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        context = self._run_context.pop(run_id, None)
        if not context or context.get("kind") != "node":
            return
        name = context["name"]
        self._put(self._emitter.emit(
            category="node",
            state="completed",
            phase="execution",
            actor=context["actor"],
            visibility="details",
            display_key=f"agent.node.{name}.completed",
            fallback_text="Workflow step completed",
            task_id=f"node_{name}",
            event_key=f"node.{name}.completed",
        ))

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        context = self._run_context.pop(run_id, None)
        if not context or context.get("kind") != "node":
            return
        name = context["name"]
        self._put(self._emitter.emit(
            category="node",
            state="failed",
            phase="execution",
            actor=context["actor"],
            visibility="timeline",
            display_key=f"agent.node.{name}.failed",
            fallback_text="Workflow step failed",
            safe_detail_summary=type(error).__name__,
            task_id=f"node_{name}",
            event_key=f"node.{name}.failed",
        ))

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        name = self._safe_name(serialized, kwargs)
        actor = _actor_for_tool(name)
        self._run_context[run_id] = {"name": name, "actor": actor, "kind": "tool"}
        self._put(self._emitter.emit(
            category="tool",
            state="started",
            phase="execution",
            actor=actor,
            visibility="details",
            display_key=f"agent.tool.{name}.started",
            fallback_text="Tool started",
            task_id=f"tool_{name}",
            event_key=f"tool.{name}.started",
        ))

    def on_tool_end(
        self,
        output: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        context = self._run_context.pop(run_id, None)
        if not context or context.get("kind") != "tool":
            return
        name = context["name"]
        self._put(self._emitter.emit(
            category="tool",
            state="completed",
            phase="execution",
            actor=context["actor"],
            visibility="details",
            display_key=f"agent.tool.{name}.completed",
            fallback_text="Tool completed",
            task_id=f"tool_{name}",
            event_key=f"tool.{name}.completed",
        ))

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> Any:
        context = self._run_context.pop(run_id, None)
        if not context or context.get("kind") != "tool":
            return
        name = context["name"]
        self._put(self._emitter.emit(
            category="tool",
            state="failed",
            phase="execution",
            actor=context["actor"],
            visibility="timeline",
            display_key=f"agent.tool.{name}.failed",
            fallback_text="Tool failed",
            safe_detail_summary=type(error).__name__,
            task_id=f"tool_{name}",
            event_key=f"tool.{name}.failed",
        ))

    def _put(self, chunk: dict[str, Any]) -> None:
        self._queue.put_nowait(chunk)

    def _safe_name(self, serialized: dict[str, Any], kwargs: dict[str, Any]) -> str:
        candidate = None
        if isinstance(serialized, dict):
            candidate = serialized.get("name")
            if not candidate:
                candidate_id = serialized.get("id")
                if isinstance(candidate_id, list) and candidate_id:
                    candidate = candidate_id[-1]
        if not candidate:
            candidate = kwargs.get("name")
        return _safe_token(str(candidate or "unknown"))


def _actor_for_tool(name: str) -> str:
    if name.startswith("ledger_commit") or name.startswith("ledger_open"):
        return "bookkeeper"
    if name.startswith("ledger_query") or "balance" in name or "price" in name:
        return "analyst"
    if "ingest" in name or "bulk" in name or "file" in name:
        return "importer"
    if "preflight" in name or "check" in name:
        return "validator"
    return "agent"
