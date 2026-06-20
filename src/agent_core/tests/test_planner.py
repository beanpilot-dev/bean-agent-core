"""Tests for planner output parsing and provider compatibility."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_core.agent import validate_model_name
from agent_core.workflow.planner import (
    PLANNER_JSON_INSTRUCTION,
    PlannerOutput,
    parse_planner_output,
    planner_node,
)


class FakePlannerLLM:
    def __init__(self, response):
        self.response = response
        self.seen_messages = None

    async def ainvoke(self, messages):
        self.seen_messages = messages
        return self.response


def test_parse_planner_output_accepts_structured_object():
    result = PlannerOutput.model_validate(
        {"tasks": [{"route": "ANALYTICS", "task": "Summarize dining spend."}]}
    )

    parsed = parse_planner_output(result)

    assert parsed.tasks[0].route == "ANALYTICS"
    assert parsed.tasks[0].task == "Summarize dining spend."


def test_parse_planner_output_accepts_json_message_text():
    result = AIMessage(
        content='{"tasks":[{"route":"CHITCHAT","task":"Answer briefly."}]}'
    )

    parsed = parse_planner_output(result)

    assert parsed.tasks[0].route == "CHITCHAT"
    assert parsed.tasks[0].task == "Answer briefly."


@pytest.mark.asyncio
async def test_planner_node_json_text_mode_adds_instruction_and_routes():
    llm = FakePlannerLLM(
        AIMessage(
            content='{"tasks":[{"route":"ANALYTICS","task":"List available accounts."}]}'
        )
    )

    result = await planner_node(
        {
            "messages": [HumanMessage(content="What accounts exist?")],
            "route": "",
            "sub_task": "",
            "original_query": "",
            "pending_routes": [],
            "had_multiple_tasks": False,
        },
        {"configurable": {"planner_llm": llm, "planner_output_mode": "json_text"}},
    )

    assert result["route"] == "analytics"
    assert result["sub_task"] == "List available accounts."
    assert PLANNER_JSON_INSTRUCTION in llm.seen_messages[0].content


def test_validate_model_name_rejects_env_file_values():
    with pytest.raises(ValueError, match="environment file"):
        validate_model_name("secrets.saas.dev.env")


def test_validate_model_name_accepts_versioned_model_names():
    assert validate_model_name("gpt-4.1") == "gpt-4.1"
    assert validate_model_name("deepseek-v4-flash") == "deepseek-v4-flash"
