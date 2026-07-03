"""Tests for the tracing module (LangFuse SDK v4, OTEL-native)."""

from unittest.mock import MagicMock, patch

import pytest

from agent_core.tracing import (
    TracingManager,
    _NoopCallback,
    _read_env,
    _safe_trace_metadata,
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

    def test_default_level_is_metadata(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_ENABLED", "true")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        monkeypatch.delenv("LANGFUSE_TRACE_LEVEL", raising=False)
        assert _read_env()["trace_level"] == "metadata"

    def test_metadata_level(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_TRACE_LEVEL", "metadata")
        assert _read_env()["trace_level"] == "metadata"

    def test_base_url_default(self, monkeypatch):
        monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
        monkeypatch.delenv("LANGFUSE_HOST", raising=False)
        assert _read_env()["base_url"] == "http://localhost:3000"

    def test_base_url_from_langfuse_base_url(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_BASE_URL", "http://langfuse:3000")
        monkeypatch.delenv("LANGFUSE_HOST", raising=False)
        assert _read_env()["base_url"] == "http://langfuse:3000"

    def test_base_url_falls_back_to_langfuse_host(self, monkeypatch):
        monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
        monkeypatch.setenv("LANGFUSE_HOST", "http://old:4000")
        assert _read_env()["base_url"] == "http://old:4000"

    def test_base_url_prefers_langfuse_base_url(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_BASE_URL", "http://new:3000")
        monkeypatch.setenv("LANGFUSE_HOST", "http://old:4000")
        assert _read_env()["base_url"] == "http://new:3000"


class TestNoopCallback:
    def test_is_base_callback_handler(self):
        assert isinstance(_NoopCallback(), object)


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


class TestTracingManagerEnabledFullMode:
    @pytest.fixture(autouse=True)
    def enable_tracing(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_ENABLED", "true")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:9999")
        monkeypatch.setenv("LANGFUSE_TRACE_LEVEL", "full")

    @patch("langfuse.langchain.CallbackHandler")
    @patch("langfuse.propagate_attributes")
    @patch("langfuse.Langfuse")
    def test_trace_yields_callback_handler(
        self, mock_langfuse_cls, mock_propagate, mock_cb_handler
    ):
        mock_client = MagicMock()
        mock_root_span = MagicMock()
        mock_root_span.trace_id = "abcdef1234567890abcdef1234567890"
        mock_client.start_as_current_observation.return_value.__enter__ = (
            MagicMock(return_value=mock_root_span)
        )
        mock_client.start_as_current_observation.return_value.__exit__ = (
            MagicMock(return_value=False)
        )
        mock_langfuse_cls.return_value = mock_client

        # Ensure propagate_attributes is a no-op context manager
        mock_propagate.return_value.__enter__ = MagicMock()
        mock_propagate.return_value.__exit__ = MagicMock(return_value=False)

        mock_handler_instance = MagicMock()
        mock_cb_handler.return_value = mock_handler_instance

        mgr = TracingManager()
        assert mgr.enabled is True

        with mgr.trace(task="agent-turn", conversation_id="conv-1") as handler:
            assert handler is mock_handler_instance
            assert mgr.get_trace_id() == "abcdef1234567890abcdef1234567890"

        # Verify root span was created with correct name
        mock_client.start_as_current_observation.assert_called_once()
        kwargs = mock_client.start_as_current_observation.call_args.kwargs
        assert kwargs["name"] == "agent-turn"
        assert kwargs["as_type"] == "span"

        # Verify attributes propagated
        mock_propagate.assert_called_once()
        attr_kwargs = mock_propagate.call_args.kwargs
        assert attr_kwargs["session_id"] == "conv-1"

        # Verify flush was called
        mock_client.flush.assert_called_once()

    @patch("langfuse.langchain.CallbackHandler")
    @patch("langfuse.propagate_attributes")
    @patch("langfuse.Langfuse")
    def test_root_span_input_is_redacted(
        self, mock_langfuse_cls, mock_propagate, mock_cb_handler
    ):
        mock_client = MagicMock()
        mock_root_span = MagicMock()
        mock_root_span.trace_id = "trace-input"
        mock_client.start_as_current_observation.return_value.__enter__ = (
            MagicMock(return_value=mock_root_span)
        )
        mock_client.start_as_current_observation.return_value.__exit__ = (
            MagicMock(return_value=False)
        )
        mock_langfuse_cls.return_value = mock_client

        mock_propagate.return_value.__enter__ = MagicMock()
        mock_propagate.return_value.__exit__ = MagicMock(return_value=False)

        mock_handler_instance = MagicMock()
        mock_cb_handler.return_value = mock_handler_instance

        mgr = TracingManager()
        with mgr.trace(task="test", input={"query": "hello"}):
            pass

        call_kwargs = mock_client.start_as_current_observation.call_args.kwargs
        assert call_kwargs["input"] is None

    @patch("langfuse.langchain.CallbackHandler")
    @patch("langfuse.propagate_attributes")
    @patch("langfuse.Langfuse")
    def test_long_metadata_dropped(
        self, mock_langfuse_cls, mock_propagate, mock_cb_handler
    ):
        mock_client = MagicMock()
        mock_root_span = MagicMock()
        mock_root_span.trace_id = "trace-trunc"
        mock_client.start_as_current_observation.return_value.__enter__ = (
            MagicMock(return_value=mock_root_span)
        )
        mock_client.start_as_current_observation.return_value.__exit__ = (
            MagicMock(return_value=False)
        )
        mock_langfuse_cls.return_value = mock_client

        mock_propagate.return_value.__enter__ = MagicMock()
        mock_propagate.return_value.__exit__ = MagicMock(return_value=False)

        mock_handler_instance = MagicMock()
        mock_cb_handler.return_value = mock_handler_instance

        long_value = "x" * 250
        mgr = TracingManager()
        with mgr.trace(task="test", model=long_value):
            pass

        attr_kwargs = mock_propagate.call_args.kwargs
        assert attr_kwargs["metadata"] is None

    @patch("langfuse.langchain.CallbackHandler")
    @patch("langfuse.propagate_attributes")
    @patch("langfuse.Langfuse")
    def test_trace_url_uses_client_method(
        self, mock_langfuse_cls, mock_propagate, mock_cb_handler
    ):
        mock_client = MagicMock()
        mock_client.get_trace_url.return_value = "http://localhost:9999/project/p123/traces/trace123"
        mock_root_span = MagicMock()
        mock_root_span.trace_id = "trace123"
        mock_client.start_as_current_observation.return_value.__enter__ = (
            MagicMock(return_value=mock_root_span)
        )
        mock_client.start_as_current_observation.return_value.__exit__ = (
            MagicMock(return_value=False)
        )
        mock_langfuse_cls.return_value = mock_client

        mock_propagate.return_value.__enter__ = MagicMock()
        mock_propagate.return_value.__exit__ = MagicMock(return_value=False)

        mock_handler_instance = MagicMock()
        mock_cb_handler.return_value = mock_handler_instance

        mgr = TracingManager()
        with mgr.trace(task="test"):
            pass

        url = mgr.get_trace_url()
        mock_client.get_trace_url.assert_called_once_with("trace123")
        assert url == "http://localhost:9999/project/p123/traces/trace123"

    @patch("langfuse.langchain.CallbackHandler")
    @patch("langfuse.propagate_attributes")
    @patch("langfuse.Langfuse")
    def test_user_id_and_metadata_propagated(
        self, mock_langfuse_cls, mock_propagate, mock_cb_handler
    ):
        mock_client = MagicMock()
        mock_root_span = MagicMock()
        mock_root_span.trace_id = "trace-xyz"
        mock_client.start_as_current_observation.return_value.__enter__ = (
            MagicMock(return_value=mock_root_span)
        )
        mock_client.start_as_current_observation.return_value.__exit__ = (
            MagicMock(return_value=False)
        )
        mock_langfuse_cls.return_value = mock_client

        mock_propagate.return_value.__enter__ = MagicMock()
        mock_propagate.return_value.__exit__ = MagicMock(return_value=False)

        mock_handler_instance = MagicMock()
        mock_cb_handler.return_value = mock_handler_instance

        mgr = TracingManager()
        with mgr.trace(
            task="agent-turn",
            user_id="user-42",
            conversation_id="conv-1",
            conversation_tag="#test",
            model="gpt-4o",
        ):
            pass

        # Verify propagate_attributes received user_id, session_id, tags, trace_name
        mock_propagate.assert_called_once()
        attr_kwargs = mock_propagate.call_args.kwargs
        assert attr_kwargs["user_id"] == "user-42"
        assert attr_kwargs["session_id"] == "conv-1"
        assert attr_kwargs["trace_name"] == "agent-turn"
        assert attr_kwargs["tags"] == ["#test"]
        # Other keys (model) go to metadata
        assert "model" in attr_kwargs["metadata"]
        assert attr_kwargs["metadata"]["model"] == "gpt-4o"
        # conversation_tag is removed from metadata (moved to tags)
        assert "conversation_tag" not in attr_kwargs["metadata"]

    def test_trace_metadata_excludes_sensitive_values(self):
        metadata = _safe_trace_metadata({
            "model": "gpt-4o",
            "repo_url": "https://github.com/private/repo",
            "transaction_text": '2026-01-01 * "Lunch"',
            "conversation_name": "Assets:Cash 100 CNY",
        })

        assert metadata == {"model": "gpt-4o"}


class TestTracingManagerEnabledMetadataMode:
    @pytest.fixture(autouse=True)
    def enable_tracing(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_ENABLED", "true")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:9999")
        monkeypatch.setenv("LANGFUSE_TRACE_LEVEL", "metadata")

    @patch("langfuse.propagate_attributes")
    @patch("langfuse.Langfuse")
    def test_trace_yields_noop_handler(self, mock_langfuse_cls, mock_propagate):
        mock_client = MagicMock()
        mock_root_span = MagicMock()
        mock_root_span.trace_id = "trace-metadata"
        mock_client.start_as_current_observation.return_value.__enter__ = (
            MagicMock(return_value=mock_root_span)
        )
        mock_client.start_as_current_observation.return_value.__exit__ = (
            MagicMock(return_value=False)
        )
        mock_langfuse_cls.return_value = mock_client

        mock_propagate.return_value.__enter__ = MagicMock()
        mock_propagate.return_value.__exit__ = MagicMock(return_value=False)

        mgr = TracingManager()
        with mgr.trace(task="agent-turn") as handler:
            assert isinstance(handler, _NoopCallback)

        assert mgr.get_trace_id() == "trace-metadata"

    @patch("langfuse.propagate_attributes")
    @patch("langfuse.Langfuse")
    def test_no_callback_handler_imported(self, mock_langfuse_cls, mock_propagate):
        mock_client = MagicMock()
        mock_root_span = MagicMock()
        mock_root_span.trace_id = "trace-meta-2"
        mock_client.start_as_current_observation.return_value.__enter__ = (
            MagicMock(return_value=mock_root_span)
        )
        mock_client.start_as_current_observation.return_value.__exit__ = (
            MagicMock(return_value=False)
        )
        mock_langfuse_cls.return_value = mock_client

        mock_propagate.return_value.__enter__ = MagicMock()
        mock_propagate.return_value.__exit__ = MagicMock(return_value=False)

        mgr = TracingManager()
        with mgr.trace(task="test"):
            pass

        mock_client.flush.assert_called_once()


class TestTracingManagerShutdown:
    @pytest.fixture(autouse=True)
    def enable_tracing(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_ENABLED", "true")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
        monkeypatch.setenv("LANGFUSE_BASE_URL", "http://localhost:9999")
        monkeypatch.setenv("LANGFUSE_TRACE_LEVEL", "full")

    @patch("langfuse.langchain.CallbackHandler")
    @patch("langfuse.propagate_attributes")
    @patch("langfuse.Langfuse")
    def test_shutdown_called(self, mock_langfuse_cls, mock_propagate, mock_cb_handler):
        mock_client = MagicMock()
        mock_root_span = MagicMock()
        mock_root_span.trace_id = "trace-shutdown"
        mock_client.start_as_current_observation.return_value.__enter__ = (
            MagicMock(return_value=mock_root_span)
        )
        mock_client.start_as_current_observation.return_value.__exit__ = (
            MagicMock(return_value=False)
        )
        mock_langfuse_cls.return_value = mock_client

        mock_propagate.return_value.__enter__ = MagicMock()
        mock_propagate.return_value.__exit__ = MagicMock(return_value=False)

        mock_handler_instance = MagicMock()
        mock_cb_handler.return_value = mock_handler_instance

        mgr = TracingManager()
        mgr.shutdown()
        mock_client.shutdown.assert_called_once()

    def test_shutdown_noop_when_disabled(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_ENABLED", "false")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
        mgr = TracingManager()
        mgr.shutdown()  # Should not raise


class TestTracingManagerSingleton:
    def test_singleton(self, monkeypatch):
        monkeypatch.setenv("LANGFUSE_ENABLED", "false")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")

        mgr1 = get_tracing_manager()
        mgr2 = get_tracing_manager()
        assert mgr1 is mgr2

        import agent_core.tracing as tracing_mod

        tracing_mod._manager = None
