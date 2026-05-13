"""Tests for the tracing module (LangFuse SDK v2)."""

from unittest.mock import MagicMock, patch

import pytest

from agent_core.tracing import (
    _REDACTED,
    TracingManager,
    _LangfuseCallback,
    _NoopCallback,
    _read_env,
    get_tracing_manager,
)


class TestReadEnv:
    def test_disabled_when_false_string(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_ENABLED", "false")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        assert _read_env()["enabled"] is False

    def test_disabled_when_no_secret_key(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_ENABLED", "true")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
        assert _read_env()["enabled"] is False

    def test_enabled_when_true_and_key_set(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_ENABLED", "true")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        assert _read_env()["enabled"] is True

    def test_default_level_is_full(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_ENABLED", "true")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        assert _read_env()["trace_level"] == "full"

    def test_metadata_level(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_TRACE_LEVEL", "metadata")
        assert _read_env()["trace_level"] == "metadata"


class TestNoopCallback:
    def test_last_trace_id_is_none(self):
        assert _NoopCallback().last_trace_id is None


class TestLangfuseCallbackFullMode:
    """Tests for _LangfuseCallback in full mode (no redaction)."""

    @pytest.fixture
    def mock_trace(self):
        t = MagicMock()
        t.trace_id = "trace-001"
        t.span.return_value = MagicMock()
        return t

    @pytest.fixture
    def callback(self, mock_trace):
        return _LangfuseCallback(trace=mock_trace, trace_level="full")

    def test_last_trace_id_set(self, mock_trace):
        cb = _LangfuseCallback(trace=mock_trace, trace_level="full")
        assert cb.last_trace_id == "trace-001"

    def test_on_chain_start_creates_turn_span(self, callback, mock_trace):
        callback.on_chain_start({"name": "LangGraph"}, {"messages": []})
        mock_trace.span.assert_called_once_with(name="agent-turn")

    def test_on_llm_start_creates_generation(self, callback, mock_trace):
        mock_span = MagicMock()
        mock_trace.span.return_value = mock_span

        callback.on_llm_start({"name": "gpt-4o"}, ["prompt"], run_id="r1")
        mock_span.generation.assert_called_once()
        call_kwargs = mock_span.generation.call_args.kwargs
        assert call_kwargs["name"] == "ChatOpenAI"
        assert call_kwargs["model"] == "gpt-4o"
        assert call_kwargs["input"] == ["prompt"]

    def test_on_llm_end_calls_generation_end(self, callback, mock_trace):
        mock_gen = MagicMock()
        mock_span = MagicMock()
        mock_span.generation.return_value = mock_gen
        mock_trace.span.return_value = mock_span

        callback.on_llm_start({"name": "gpt-4o"}, ["prompt"], run_id="r1")

        token_usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        response = type("Response", (), {
            "llm_output": {"token_usage": token_usage},
            "generations": [[type("Gen", (), {"text": "response text", "message": None})()]],
        })()
        callback.on_llm_end(response, run_id="r1")

        mock_gen.end.assert_called_once()
        assert mock_gen.end.call_args.kwargs["output"] == "response text"

    def test_on_tool_start_creates_span(self, callback, mock_trace):
        mock_turn_span = MagicMock()
        mock_trace.span.return_value = mock_turn_span

        callback.on_tool_start({"name": "ledger_commit"}, '{"amount": 100}', run_id="r1")
        mock_turn_span.span.assert_called_once()
        call_kwargs = mock_turn_span.span.call_args.kwargs
        assert call_kwargs["name"] == "ledger_commit"
        assert call_kwargs["input"] == '{"amount": 100}'

    def test_on_tool_end_calls_span_end(self, callback, mock_trace):
        mock_tool_span = MagicMock()
        mock_turn_span = MagicMock()
        mock_turn_span.span.return_value = mock_tool_span
        mock_trace.span.return_value = mock_turn_span

        callback.on_tool_start({"name": "ledger_commit"}, "{}", run_id="r1")
        callback.on_tool_end("SUCCESS", run_id="r1")

        mock_tool_span.end.assert_called_once_with(output="SUCCESS")

    def test_on_tool_error_reports_error(self, callback, mock_trace):
        mock_tool_span = MagicMock()
        mock_turn_span = MagicMock()
        mock_turn_span.span.return_value = mock_tool_span
        mock_trace.span.return_value = mock_turn_span

        callback.on_tool_start({"name": "ledger_commit"}, "{}", run_id="r1")
        callback.on_tool_error(ValueError("bad"), run_id="r1")

        kwargs = mock_tool_span.end.call_args.kwargs
        assert kwargs["level"] == "ERROR"
        assert "bad" in kwargs["status_message"]


class TestLangfuseCallbackMetadataMode:
    """Tests for _LangfuseCallback in metadata mode (redaction)."""

    @pytest.fixture
    def mock_trace(self):
        t = MagicMock()
        t.trace_id = "trace-002"
        return t

    @pytest.fixture
    def callback(self, mock_trace):
        return _LangfuseCallback(trace=mock_trace, trace_level="metadata")

    def test_llm_input_redacted(self, callback, mock_trace):
        mock_span = MagicMock()
        mock_trace.span.return_value = mock_span

        callback.on_llm_start({"name": "gpt-4o"}, ["sensitive prompt"], run_id="r1")
        call_kwargs = mock_span.generation.call_args.kwargs
        assert call_kwargs["input"] == [_REDACTED]

    def test_tool_input_redacted(self, callback, mock_trace):
        mock_turn_span = MagicMock()
        mock_trace.span.return_value = mock_turn_span

        callback.on_tool_start({"name": "ledger_commit"}, '{"account":"Bank"}', run_id="r1")
        call_kwargs = mock_turn_span.span.call_args.kwargs
        assert call_kwargs["input"] == _REDACTED

    def test_tool_output_redacted(self, callback, mock_trace):
        mock_tool_span = MagicMock()
        mock_turn_span = MagicMock()
        mock_turn_span.span.return_value = mock_tool_span
        mock_trace.span.return_value = mock_turn_span

        callback.on_tool_start({"name": "ledger_query"}, "{}", run_id="r1")
        callback.on_tool_end('{"balance": 1000}', run_id="r1")

        assert mock_tool_span.end.call_args.kwargs["output"] == _REDACTED


class TestTracingManagerDisabled:
    @pytest.fixture(autouse=True)
    def disable_tracing(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_ENABLED", "false")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")

    def test_disabled(self):
        mgr = TracingManager()
        assert mgr.enabled is False

    def test_noop_handler_returned(self):
        mgr = TracingManager()
        with mgr.trace(task="test") as handler:
            assert isinstance(handler, _NoopCallback)

    def test_trace_id_and_url_are_none(self):
        mgr = TracingManager()
        assert mgr.get_trace_id() is None
        assert mgr.get_trace_url() is None


class TestTracingManagerEnabled:
    @pytest.fixture(autouse=True)
    def enable_tracing(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_ENABLED", "true")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:9999")
        monkeypatch.setenv("LANGFUSE_TRACE_LEVEL", "full")

    @patch("langfuse.Langfuse")
    def test_trace_creates_callback(self, mock_langfuse_cls):
        mock_client = MagicMock()
        mock_trace = MagicMock()
        mock_trace.trace_id = "trace-mock-001"
        mock_client.trace.return_value = mock_trace
        mock_langfuse_cls.return_value = mock_client

        mgr = TracingManager()
        with mgr.trace(task="test", conversation_id="conv-1") as handler:
            assert isinstance(handler, _LangfuseCallback)
            assert handler.last_trace_id == "trace-mock-001"

    @patch("langfuse.Langfuse")
    def test_trace_metadata_passed(self, mock_langfuse_cls):
        mock_client = MagicMock()
        mock_trace = MagicMock()
        mock_trace.trace_id = "trace-mock-002"
        mock_client.trace.return_value = mock_trace
        mock_langfuse_cls.return_value = mock_client

        mgr = TracingManager()
        with mgr.trace(task="agent-turn", conversation_id="conv-1", conversation_tag="#test"):
            pass

        mock_client.trace.assert_called_once_with(name="agent-turn")
        mock_trace.update.assert_called_once()
        metadata_passed = mock_trace.update.call_args.kwargs["metadata"]
        assert metadata_passed["conversation_id"] == "conv-1"
        assert metadata_passed["conversation_tag"] == "#test"

    @patch("langfuse.Langfuse")
    def test_flush_called_on_exit(self, mock_langfuse_cls):
        mock_client = MagicMock()
        mock_trace = MagicMock()
        mock_client.trace.return_value = mock_trace
        mock_langfuse_cls.return_value = mock_client

        mgr = TracingManager()
        with mgr.trace(task="test"):
            pass

        mock_client.flush.assert_called_once()


class TestTracingManagerSingleton:
    def test_singleton(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_ENABLED", "false")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")

        mgr1 = get_tracing_manager()
        mgr2 = get_tracing_manager()
        assert mgr1 is mgr2

        import agent_core.tracing as tracing_mod
        tracing_mod._manager = None
