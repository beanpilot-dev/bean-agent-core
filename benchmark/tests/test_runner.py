"""Tests for BeanBench runner environment construction."""

from beanbench.runner import RunConfig, _build_agent_env


def test_langfuse_enabled_benchmark_uses_full_trace_level():
    config = RunConfig(
        model="test-model",
        api_key="test-key",
        langfuse_enabled=True,
        langfuse_public_key="test-public-key",
        langfuse_secret_key="test-secret-key",
        langfuse_base_url="http://langfuse.test",
    )

    env = _build_agent_env(config)

    assert env["LANGFUSE_ENABLED"] == "true"
    assert env["LANGFUSE_TRACE_LEVEL"] == "full"


def test_langfuse_disabled_benchmark_does_not_set_trace_level():
    config = RunConfig(model="test-model", api_key="test-key")

    env = _build_agent_env(config)

    assert "LANGFUSE_ENABLED" not in env
    assert "LANGFUSE_TRACE_LEVEL" not in env
