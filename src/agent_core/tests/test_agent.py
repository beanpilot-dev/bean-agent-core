"""Tests for PersonalFinanceAgent response classification."""

import asyncio
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agent_core.agent import PersonalFinanceAgent, normalize_conversation_title
from agent_core.services.activity import ActivityCallbackHandler, ActivityEmitter
from agent_core.workflow.language import response_language_instruction


@pytest.mark.parametrize(
    "content",
    [
        "Please confirm the transaction looks right.",
        "I cannot approve that account.",
        "Shall I record an explanation instead?",
        "Confirm this code block is valid.",
        "Do you want me to proceed with the analysis?",
    ],
)
def test_requires_user_input_ignores_confirmation_phrases(content):
    result = {"messages": [AIMessage(content=content)]}

    assert PersonalFinanceAgent._requires_user_input(result) is False


def test_requires_user_input_returns_false_without_messages():
    assert PersonalFinanceAgent._requires_user_input({}) is False


class FailingGraph:
    async def ainvoke(self, _input, config=None):
        callbacks = (config or {}).get("callbacks", [])
        for callback in callbacks:
            if isinstance(callback, ActivityCallbackHandler):
                run_id = uuid4()
                callback._run_context[run_id] = {
                    "name": "ledger_commit",
                    "actor": "bookkeeper",
                    "kind": "tool",
                }
                callback.on_tool_error(RuntimeError("raw failure"), run_id=run_id)
        raise RuntimeError("graph failed")


class FakeLLM:
    def bind_tools(self, _tools):
        return self

    def with_structured_output(self, _schema, method=None):
        return self


class CapturingLLM:
    bound_tool_names: list[str] = []

    def bind_tools(self, tools):
        CapturingLLM.bound_tool_names = [tool.name for tool in tools]
        return self

    async def ainvoke(self, _messages, config=None):
        return AIMessage(content="single loop response")


@pytest.mark.asyncio
async def test_stream_drains_activity_queue_when_graph_fails(monkeypatch):
    async def fake_sleep(_delay):
        return None

    agent = PersonalFinanceAgent()
    agent.graph = FailingGraph()
    emitter = ActivityEmitter(run_id="run_test")
    chunks = []

    monkeypatch.setattr("agent_core.agent.validate_model_name", lambda model: model)
    monkeypatch.setattr("agent_core.agent.ChatOpenAI", lambda **_kwargs: FakeLLM())
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async for chunk in agent.stream(
        query="fail",
        prior=[],
        api_key="key",
        model="scripted",
        activity_emitter=emitter,
    ):
        chunks.append(chunk)

    assert any(
        chunk.get("type") == "activity"
        and chunk.get("category") == "tool"
        and chunk.get("state") == "failed"
        for chunk in chunks
    )
    assert any(
        chunk.get("type") == "activity"
        and chunk.get("category") == "node"
        and chunk.get("state") == "failed"
        for chunk in chunks
    )


def test_requires_user_input_detects_preview_string_content():
    result = {
        "messages": [
            ToolMessage(
                content='{"status": "PREVIEW", "proposed": "transaction"}',
                tool_call_id="preview-1",
            )
        ]
    }

    assert PersonalFinanceAgent._requires_user_input(result) is True


@pytest.mark.parametrize(
    "part",
    [
        {"status": "PREVIEW", "proposed": "transaction"},
        {
            "type": "text",
            "text": '{"status": "PREVIEW", "proposed": "transaction"}',
        },
    ],
)
def test_requires_user_input_detects_preview_list_content(part):
    result = {
        "messages": [
            ToolMessage(
                content=[part],
                tool_call_id="preview-1",
            )
        ]
    }

    assert PersonalFinanceAgent._requires_user_input(result) is True


def test_requires_user_input_ignores_non_preview_structured_content():
    result = {
        "messages": [
            ToolMessage(
                content=[
                    {
                        "status": "SUCCESS",
                        "message": "Please confirm receipt.",
                    }
                ],
                tool_call_id="confirm-1",
            )
        ]
    }

    assert PersonalFinanceAgent._requires_user_input(result) is False


def test_requires_user_input_ignores_quoted_preview_in_assistant_text():
    result = {
        "messages": [
            AIMessage(content='A preview response uses: {"status": "PREVIEW"}'),
        ]
    }

    assert PersonalFinanceAgent._requires_user_input(result) is False


@pytest.mark.parametrize(
    "text",
    [
        'A preview response uses: {"status": "PREVIEW"}',
        '```json\n{"status": "PREVIEW"}\n```',
    ],
)
def test_requires_user_input_ignores_quoted_preview_in_tool_text_block(text):
    result = {
        "messages": [
            ToolMessage(
                content=[{"type": "text", "text": text}],
                tool_call_id="example-1",
            )
        ]
    }

    assert PersonalFinanceAgent._requires_user_input(result) is False


def test_response_language_instruction_preserves_ledger_literals():
    instruction = response_language_instruction("zh-CN")

    assert "Simplified Chinese" in instruction
    assert "Beancount syntax" in instruction
    assert "account names" in instruction
    assert "machine-readable codes" in instruction


def test_normalize_conversation_title_strips_markup_and_punctuation():
    title = normalize_conversation_title(' "**Trip budget planning!**" ')

    assert title == "Trip budget planning"


def test_normalize_conversation_title_rejects_table_like_output():
    assert normalize_conversation_title("| title |") == ""


def test_default_model_manifest_excludes_confirm_tools():
    agent = PersonalFinanceAgent()
    tool_names = [tool.name for tool in agent.model_tools]

    assert "confirm_commit" not in tool_names
    assert "confirm_open" not in tool_names
    assert "confirm_update" not in tool_names
    assert "confirm_bulk" not in tool_names
    assert "prepare_commit" in tool_names


def test_default_graph_has_no_planner_nodes():
    agent = PersonalFinanceAgent()
    graph = agent.graph.get_graph()
    node_names = set(graph.nodes.keys())
    edges = {(edge.source, edge.target) for edge in graph.edges}

    assert node_names == {"__start__", "agent", "tools", "__end__"}
    assert ("__start__", "agent") in edges
    assert ("tools", "agent") in edges
    assert "planner" not in node_names
    assert "synthesizer" not in node_names
