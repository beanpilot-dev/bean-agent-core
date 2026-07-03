"""Tests for PersonalFinanceAgent response classification."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent_core.agent import (
    PersonalFinanceAgent,
    _pending_actions,
    _single_agent_node,
    normalize_conversation_title,
)
from agent_core.services.activity import ActivityCallbackHandler, ActivityEmitter
from agent_core.workflow.language import (
    detect_preferred_language,
    response_language_instruction,
)


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


class PromptCapturingLLM:
    messages: list | None = None

    async def ainvoke(self, messages, config=None):
        self.messages = messages
        return AIMessage(content="node response")


class StreamingLLM:
    async def astream(self, _messages, config=None):
        yield AIMessageChunk(content="streamed ")
        yield AIMessageChunk(content="response")

    async def ainvoke(self, _messages, config=None):
        raise AssertionError("streaming path should not fall back to ainvoke")


class CapturingGraph:
    captured_input: dict | None = None
    captured_config: dict | None = None

    async def ainvoke(self, graph_input, config=None):
        self.captured_input = graph_input
        self.captured_config = config
        return {
            "messages": graph_input["messages"] + [AIMessage(content="captured response")],
        }


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


@pytest.mark.asyncio
async def test_single_agent_node_streams_content_to_queue():
    queue: asyncio.Queue[str] = asyncio.Queue()

    result = await _single_agent_node(
        {
            "messages": [HumanMessage(content="Summarize cash")],
            "preferred_language": "en",
        },
        {
            "configurable": {
                "single_loop_llm": StreamingLLM(),
                "content_stream_queue": queue,
                "today": "2026-07-01",
            }
        },
    )

    assert result["messages"][0].content == "streamed response"
    assert queue.get_nowait() == "streamed "
    assert queue.get_nowait() == "response"


def test_requires_user_input_ignores_legacy_preview_string_content():
    result = {
        "messages": [
            ToolMessage(
                content='{"status": "PREVIEW", "proposed": "transaction"}',
                tool_call_id="preview-1",
            )
        ]
    }

    assert PersonalFinanceAgent._requires_user_input(result) is False


def test_requires_user_input_detects_pending_action_string_content():
    result = {
        "messages": [
            ToolMessage(
                content='{"status": "PENDING_ACTION", "proposed": "transaction"}',
                tool_call_id="pending-action-1",
            )
        ]
    }

    assert PersonalFinanceAgent._requires_user_input(result) is True


def test_requires_user_input_detects_gateway_approval_required_string_content():
    result = {
        "messages": [
            ToolMessage(
                content='{"status": "approval_required", "pending_action": {}}',
                tool_call_id="pending-action-1",
            )
        ]
    }

    assert PersonalFinanceAgent._requires_user_input(result) is True


def test_gateway_approval_required_streams_inner_pending_action() -> None:
    result = {
        "messages": [
            ToolMessage(
                content=(
                    '{"status": "approval_required", '
                    '"pending_action": {"status": "PENDING_ACTION", "pending_action_id": "pa_1"}}'
                ),
                tool_call_id="pending-action-1",
            )
        ]
    }

    assert _pending_actions(result) == [
        {"status": "PENDING_ACTION", "pending_action_id": "pa_1"}
    ]


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
def test_requires_user_input_ignores_legacy_preview_list_content(part):
    result = {
        "messages": [
            ToolMessage(
                content=[part],
                tool_call_id="preview-1",
            )
        ]
    }

    assert PersonalFinanceAgent._requires_user_input(result) is False


@pytest.mark.parametrize(
    "part",
    [
        {"status": "PENDING_ACTION", "proposed": "transaction"},
        {"status": "approval_required", "pending_action": {}},
        {
            "type": "text",
            "text": '{"status": "PENDING_ACTION", "proposed": "transaction"}',
        },
        {
            "type": "text",
            "text": '{"status": "approval_required", "pending_action": {}}',
        },
    ],
)
def test_requires_user_input_detects_pending_action_list_content(part):
    result = {
        "messages": [
            ToolMessage(
                content=[part],
                tool_call_id="pending-action-1",
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


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("How much cash can I spend from Assets:现金 and Assets:Bank:Checking?", "en"),
        ("帮我看看 Assets:Bank:Checking 还有多少钱可以花", "zh-CN"),
        (
            "请用 Expenses:Food:Dining 记录 2026-06-29 coffee 12.50 USD",
            "zh-CN",
        ),
        ("Assets:现金 USD 2026-06-29", "auto"),
    ],
)
def test_detect_preferred_language_uses_latest_user_prose_not_ledger_literals(
    prompt,
    expected,
):
    assert detect_preferred_language(prompt) == expected


@pytest.mark.asyncio
async def test_stream_sets_preferred_language_before_graph_invocation(monkeypatch):
    graph = CapturingGraph()
    agent = PersonalFinanceAgent()
    agent.graph = graph
    chunks = []

    monkeypatch.setattr("agent_core.agent.validate_model_name", lambda model: model)
    monkeypatch.setattr("agent_core.agent.ChatOpenAI", lambda **_kwargs: FakeLLM())

    async for chunk in agent.stream(
        query="请查询 Assets:Bank:Checking 的可用现金",
        prior=[],
        api_key="key",
        model="scripted",
    ):
        chunks.append(chunk)

    assert graph.captured_input is not None
    assert graph.captured_input["preferred_language"] == "zh-CN"
    assert chunks[-2]["content"] == "captured response"


@pytest.mark.asyncio
async def test_stream_includes_preflight_ledger_context_in_system_prompt(monkeypatch):
    graph = CapturingGraph()
    agent = PersonalFinanceAgent()
    agent.graph = graph
    ledger_context = {
        "status": "CLEAN",
        "target": "data/agent_inc/2026-06.beancount",
        "accounts": ["Assets:Liquid:Bank:Checking"],
        "recent": "",
        "errors": None,
    }

    monkeypatch.setattr("agent_core.agent.validate_model_name", lambda model: model)
    monkeypatch.setattr("agent_core.agent.ChatOpenAI", lambda **_kwargs: FakeLLM())

    async for _chunk in agent.stream(
        query="How much cash is available?",
        prior=[],
        api_key="key",
        model="scripted",
        ledger_context=ledger_context,
    ):
        pass

    assert graph.captured_config is not None
    assert graph.captured_config["configurable"]["ledger_context"] == ledger_context
    assert graph.captured_input is not None


@pytest.mark.asyncio
async def test_single_agent_node_adds_ledger_context_to_system_prompt():
    llm = PromptCapturingLLM()
    state = {
        "messages": [
            SystemMessage(content="old"),
            HumanMessage(content="How much cash is available?"),
        ],
        "preferred_language": "en",
    }
    config = {
        "configurable": {
            "single_loop_llm": llm,
            "today": "2026-06-29",
            "ledger_context": {
                "status": "CLEAN",
                "target": "data/agent_inc/2026-06.beancount",
                "accounts": ["Assets:Liquid:Bank:Checking"],
            },
        }
    }

    await _single_agent_node(state, config)

    assert llm.messages is not None
    system_prompt = llm.messages[0].content
    assert "LEDGER CONTEXT" in system_prompt
    assert "Assets:Liquid:Bank:Checking" in system_prompt


def test_single_loop_prompt_analysis_contract_has_no_global_cny_default():
    prompt_path = Path(__file__).parents[1] / "ledger" / "prompt.md"
    prompt = prompt_path.read_text()

    assert "There is no global default currency" in prompt
    assert "Always use primary currency CNY" not in prompt
    assert "spendable cash separate from net worth" in prompt
    assert "Do not present net worth as spendable cash" in prompt


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
    assert "ledger_commit_transaction" in tool_names
    assert "ledger_update_transaction" in tool_names
    assert "ledger_import_transactions" in tool_names
    assert "prepare_commit" not in tool_names


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
