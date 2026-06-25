"""
LLM tracing and observability via LangFuse SDK v4 (OTEL-native).

Supports two modes, controlled by LANGFUSE_TRACE_LEVEL:
  - "full"     -> all LLM inputs/outputs and tool inputs/outputs are recorded
                  via the official langfuse.langchain.CallbackHandler
  - "metadata" -> only the root trace span is recorded (no LLM/tool detail)
                  constraint #17 compliant — no financial data leakage

When LANGFUSE_ENABLED is "false" or LANGFUSE_SECRET_KEY is not set,
all tracing operations are no-ops.
"""

import logging
import os
from contextlib import contextmanager
from typing import Any

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

    base_url = os.environ.get("LANGFUSE_BASE_URL", "")
    if not base_url:
        base_url = os.environ.get("LANGFUSE_HOST", "http://localhost:3000")

    return {
        "enabled": enabled and bool(secret),
        "base_url": base_url,
        "public_key": os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
        "secret_key": secret,
        "trace_level": os.environ.get("LANGFUSE_TRACE_LEVEL", "full"),
    }


class _NoopCallback(BaseCallbackHandler):
    """Does nothing — used when trace_level=metadata (root span only)."""


class TracingManager:
    """Manages LangFuse tracing for agent runs (SDK v4, OTEL-native).

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
        self._root_span: Any = None
        self._trace_id: str | None = None

        if self._config["enabled"]:
            try:
                from langfuse import Langfuse

                self._client = Langfuse(
                    public_key=self._config["public_key"],
                    secret_key=self._config["secret_key"],
                    base_url=self._config["base_url"],
                )
                logger.info(
                    "LangFuse tracing enabled (base_url=%s, level=%s)",
                    self._config["base_url"],
                    self._config["trace_level"],
                )
            except Exception as e:
                logger.warning(
                    "Failed to initialize LangFuse: %s — tracing disabled", e
                )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def trace_level(self) -> str:
        return self._config["trace_level"]

    def get_trace_id(self) -> str | None:
        """Return the current trace ID (W3C format, 32-char hex)."""
        return self._trace_id

    def get_trace_url(self) -> str | None:
        """Return the LangFuse web URL for the current trace."""
        if not self._client or not self._trace_id:
            return None
        try:
            return self._client.get_trace_url(self._trace_id)
        except Exception:
            return None

    def flush(self) -> None:
        """Flush pending trace data to LangFuse."""
        if self._client:
            try:
                self._client.flush()
            except Exception as e:
                logger.warning("LangFuse flush failed: %s", e)

    def shutdown(self) -> None:
        """Gracefully shut down the Langfuse client (flush + thread cleanup).

        Recommended for short-lived processes (scripts, workers, serverless)
        to prevent data loss. The SDK auto-registers an atexit hook, but
        manual invocation is safer in environments where atexit may not fire.
        """
        if self._client:
            try:
                self._client.shutdown()
            except Exception as e:
                logger.warning("LangFuse shutdown failed: %s", e)

    @contextmanager
    def trace(self, **metadata):
        """Context manager: creates a LangFuse root span, returns a callback handler.

        In full mode, the handler is langfuse.langchain.CallbackHandler which
        auto-creates nested observations for LLM and tool calls within the
        LangGraph run. In metadata mode, a noop handler is returned — only
        the root span is recorded.

        Supported kwargs:
          task: trace/span name (default: "agent-run")
          input: input data for the root observation (optional)
          user_id: user identifier for propagate_attributes
          conversation_id: session identifier for propagate_attributes
          conversation_tag: passed as a tag for filtering
          All other kwargs become metadata values (str, <=200 chars).
        """
        self._root_span = None
        self._trace_id = None

        if not self.enabled:
            yield _NoopCallback()
            return

        try:
            from langfuse import propagate_attributes

            if self._config["trace_level"] == "full":
                from langfuse.langchain import CallbackHandler as LangfuseCallback

                handler = LangfuseCallback()
            else:
                handler = _NoopCallback()

            task_name = metadata.pop("task", "agent-run")
            root_input = metadata.pop("input", None)
            user_id = metadata.pop("user_id", None)
            session_id = metadata.pop("conversation_id", None)
            conversation_tag = metadata.pop("conversation_tag", None)

            tags: list[str] | None = None
            if conversation_tag:
                tags = [conversation_tag]

            trace_metadata: dict[str, str] | None = None
            if metadata:
                trace_metadata = {}
                for k, v in metadata.items():
                    if v is not None:
                        val = str(v)
                        if len(val) > 200:
                            val = val[:197] + "..."
                        trace_metadata[k] = val

            with self._client.start_as_current_observation(
                as_type="span",
                name=task_name,
                input=root_input,
            ) as root_span:
                self._root_span = root_span
                self._trace_id = root_span.trace_id

                with propagate_attributes(
                    user_id=user_id,
                    session_id=session_id,
                    tags=tags,
                    metadata=trace_metadata,
                    trace_name=task_name,
                ):
                    yield handler

        except Exception as e:
            logger.warning(
                "LangFuse trace error: %s — falling back to noop", e
            )
            yield _NoopCallback()
        finally:
            self.flush()


_manager: TracingManager | None = None


def get_tracing_manager() -> TracingManager:
    """Return the global TracingManager singleton."""
    global _manager
    if _manager is None:
        _manager = TracingManager()
    return _manager
