"""
LLM tracing and observability via LangFuse (SDK v2, server v2).

Supports two modes, controlled by LANGFUSE_TRACE_LEVEL:
  - "full"     -> all LLM inputs/outputs and tool inputs/outputs are recorded
  - "metadata" -> only token counts, timing, tool names, and error types are recorded
                  (constraint #17 compliant — no financial data leakage)

When LANGFUSE_ENABLED is "false" or LANGFUSE_SECRET_KEY is not set,
all tracing operations are no-ops.
"""

import logging
import os
import time
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from langchain_core.callbacks.base import BaseCallbackHandler

logger = logging.getLogger(__name__)


def _read_env() -> dict:
    """Read tracing configuration from environment variables at construction time."""
    secret = os.environ.get("LANGFUSE_SECRET_KEY", "")
    enabled_str = os.environ.get("LANGFUSE_ENABLED", "").lower()
    if enabled_str in ("1", "true", "yes"):
        enabled = True
    elif enabled_str in ("0", "false", "no"):
        enabled = False
    else:
        enabled = True
    return {
        "enabled": enabled and bool(secret),
        "host": os.environ.get("LANGFUSE_HOST", "http://localhost:3000"),
        "public_key": os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
        "secret_key": secret,
        "trace_level": os.environ.get("LANGFUSE_TRACE_LEVEL", "full"),
    }


class _NoopCallback(BaseCallbackHandler):
    """Does nothing — used when tracing is disabled."""

    @property
    def last_trace_id(self) -> str | None:
        return None


_REDACTED = "[REDACTED — financial content]"


class _LangfuseCallback(BaseCallbackHandler):
    """Custom LangChain callback that records to a LangFuse SDK v2 trace.

    Creates a span for the whole agent turn, generations for LLM calls,
    and child spans for tool calls — all under the parent trace.
    """

    def __init__(
        self,
        trace: Any,
        trace_level: str = "full",
    ):
        super().__init__()
        self._trace = trace
        self._trace_level = trace_level
        self._turn_span: Any = None
        self._generations: dict[UUID, Any] = {}
        self._tool_spans: dict[UUID, Any] = {}
        self._llm_timers: dict[UUID, float] = {}
        self._tool_timers: dict[UUID, float] = {}
        self.last_trace_id: str | None = trace.trace_id if hasattr(trace, "trace_id") else None

    def _maybe_redact(self, value: Any) -> Any:
        if self._trace_level == "full":
            return value
        if isinstance(value, str):
            return _REDACTED
        if isinstance(value, (list, tuple)):
            return [_REDACTED] * len(value)
        if isinstance(value, dict):
            return {k: _REDACTED for k in value}
        return _REDACTED

    def _ensure_turn_span(self):
        if self._turn_span is None:
            self._turn_span = self._trace.span(name="agent-turn")

    # -- Chain callbacks --

    def on_chain_start(self, serialized, inputs, **kwargs):
        self._ensure_turn_span()

    def on_chain_end(self, outputs, **kwargs):
        pass

    def on_chain_error(self, error, **kwargs):
        pass

    # -- LLM callbacks --

    def on_llm_start(self, serialized, prompts, **kwargs):
        self._ensure_turn_span()
        run_id = kwargs.get("run_id")
        if run_id:
            self._llm_timers[run_id] = time.monotonic()
        model_name = (
            serialized.get("name", "unknown")
            if isinstance(serialized, dict)
            else str(serialized)
        )
        gen = self._turn_span.generation(
            name="ChatOpenAI",
            model=model_name,
            input=self._maybe_redact(prompts),
        )
        if run_id:
            self._generations[run_id] = gen

    def on_llm_end(self, response, **kwargs):
        run_id = kwargs.get("run_id")
        gen = self._generations.pop(run_id, None) if run_id else None
        if gen is None:
            return

        usage = None
        output = None
        try:
            llm_output = getattr(response, "llm_output", {}) or {}
            token_usage = llm_output.get("token_usage", {}) or {}
            if token_usage:
                from langfuse.model import ModelUsage
                usage = ModelUsage(
                    prompt_tokens=token_usage.get("prompt_tokens", 0),
                    completion_tokens=token_usage.get("completion_tokens", 0),
                    total_tokens=token_usage.get("total_tokens", 0),
                )

            generations = getattr(response, "generations", None)
            if generations and len(generations) > 0 and len(generations[0]) > 0:
                msg = getattr(generations[0][0], "message", None)
                if msg:
                    output = self._maybe_redact(msg.content)
                else:
                    output = self._maybe_redact(str(generations[0][0].text))
            else:
                output = self._maybe_redact(str(response))
        except Exception:
            output = self._maybe_redact(str(response))

        gen.end(output=output, usage=usage)

    def on_llm_error(self, error, **kwargs):
        run_id = kwargs.get("run_id")
        gen = self._generations.pop(run_id, None) if run_id else None
        if gen:
            gen.end(level="ERROR", status_message=str(error))

    # -- Tool callbacks --

    def on_tool_start(self, serialized, input_str, **kwargs):
        self._ensure_turn_span()
        run_id = kwargs.get("run_id")
        if run_id:
            self._tool_timers[run_id] = time.monotonic()
        tool_name = (
            serialized.get("name", "unknown")
            if isinstance(serialized, dict)
            else str(serialized)
        )
        span = self._turn_span.span(
            name=tool_name,
            input=self._maybe_redact(input_str),
        )
        if run_id:
            self._tool_spans[run_id] = span

    def on_tool_end(self, output, **kwargs):
        run_id = kwargs.get("run_id")
        span = self._tool_spans.pop(run_id, None) if run_id else None
        if span:
            span.end(output=self._maybe_redact(output))

    def on_tool_error(self, error, **kwargs):
        run_id = kwargs.get("run_id")
        span = self._tool_spans.pop(run_id, None) if run_id else None
        if span:
            span.end(level="ERROR", status_message=str(error))


class TracingManager:
    """Manages LangFuse tracing for agent runs (SDK v2 compatible).

    Usage:
        manager = TracingManager()

        with manager.trace(conversation_id="abc", task="record expense") as handler:
            config = {"callbacks": [handler]}
            result = await graph.ainvoke(input, config=config)

        trace_id = manager.get_trace_id()
        trace_url = manager.get_trace_url()
    """

    def __init__(self):
        self._config = _read_env()
        self._client: Any = None
        self._trace: Any = None
        self._handler: _LangfuseCallback | _NoopCallback | None = None
        self._trace_id: str | None = None

        if self._config["enabled"]:
            try:
                from langfuse import Langfuse
                self._client = Langfuse(
                    public_key=self._config["public_key"],
                    secret_key=self._config["secret_key"],
                    host=self._config["host"],
                )
                logger.info(
                    "LangFuse tracing enabled (host=%s, level=%s)",
                    self._config["host"],
                    self._config["trace_level"],
                )
            except Exception as e:
                logger.warning("Failed to initialize LangFuse: %s — tracing disabled", e)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def trace_level(self) -> str:
        return self._config["trace_level"]

    def get_trace_id(self) -> str | None:
        """Return the current trace ID, or None if tracing is disabled."""
        if self._handler and hasattr(self._handler, "last_trace_id"):
            return self._handler.last_trace_id
        return self._trace_id

    def get_trace_url(self) -> str | None:
        """Return the LangFuse web URL for the current trace."""
        trace_id = self.get_trace_id()
        if not trace_id or not self._trace:
            return None
        try:
            return self._trace.get_trace_url()
        except Exception:
            return None

    def flush(self) -> None:
        """Flush pending trace data to LangFuse."""
        if self._client:
            try:
                self._client.flush()
            except Exception as e:
                logger.warning("LangFuse flush failed: %s", e)

    @contextmanager
    def trace(self, **metadata):
        """Context manager: creates a LangFuse trace, returns a callback handler.

        The handler is ready to pass to LangGraph's ainvoke() callbacks.
        """
        self._handler = None
        self._trace = None
        self._trace_id = None

        if not self.enabled:
            yield _NoopCallback()
            return

        try:
            trace_name = metadata.get("task", "agent-run")
            trace_metadata = {k: v for k, v in metadata.items() if v is not None}

            self._trace = self._client.trace(name=trace_name)
            self._trace.update(metadata=trace_metadata)
            self._trace_id = self._trace.trace_id

            self._handler = _LangfuseCallback(
                trace=self._trace,
                trace_level=self._config["trace_level"],
            )

            yield self._handler
        except Exception as e:
            logger.warning("LangFuse trace error: %s — falling back to noop", e)
            self._handler = _NoopCallback()
            yield self._handler
        finally:
            self.flush()


_manager: TracingManager | None = None


def get_tracing_manager() -> TracingManager:
    """Return the global TracingManager singleton."""
    global _manager
    if _manager is None:
        _manager = TracingManager()
    return _manager
