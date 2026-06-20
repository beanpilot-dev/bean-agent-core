"""Tests for PersonalFinanceAgent response classification."""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from agent_core.agent import PersonalFinanceAgent, normalize_conversation_title
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
