"""Planner Node — task decomposition before pillar dispatch.

The planner uses a structured-output LLM call to decompose the user's
request into one or more sub-tasks, each with a route and a focused
task description. It reads the full conversation context (last N
messages) and the system prompt to understand available tools.

Each pillar receives only its assigned sub-task (not the full user
prompt), avoiding cross-pillar ambiguity. A synthesizer node composes
the final response from all pillar outputs.
"""

import json
import re
from typing import Any, Literal

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from pydantic import BaseModel, Field

from .state import AgentState

PLANNER_WINDOW_SIZE = 4

PLANNER_PROMPT = """You are a task planner for a personal finance assistant that manages a
Beancount double-entry ledger. Your job is to decompose the user's request into one
or more focused sub-tasks.

Available worker pillars and the tools they have:

TRANSACTION — recording, modifying, or deleting financial transactions (write tools + preflight).
ANALYTICS — querying spending, balances, reports, trends, prices (read/query tools only).
INGESTION — batch importing from CSV/TSV files (file reading + Python sandbox + bulk commit).
CHITCHAT — general questions, help, greetings, onboarding (no tools).

Rules:
- If the user asks ONE thing, return a single sub-task.
- If the user asks MULTIPLE things (e.g. "how much did I spend AND record coffee"),
  return one sub-task per pillar, ordered logically.
- Each sub-task MUST be a self-contained instruction scoped ONLY to that pillar's work.
- Strip out parts of the user's message that don't belong to the sub-task's pillar.
- Include relevant details (amounts, dates, account names) so the pillar doesn't
  need the original message.

Output a JSON object with a "tasks" key containing the list."""

PLANNER_JSON_INSTRUCTION = """Return only valid JSON. Do not include markdown fences,
comments, or explanatory text. The JSON shape must be:
{"tasks":[{"route":"CHITCHAT","task":"Answer this general question helpfully."}]}"""


class SubTask(BaseModel):
    route: Literal["TRANSACTION", "ANALYTICS", "INGESTION", "CHITCHAT"]
    task: str = Field(description="Self-contained instruction for this pillar only")


class PlannerOutput(BaseModel):
    tasks: list[SubTask]


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return str(content)


def _extract_json_object(text: str) -> dict:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("planner output must be a JSON object")
    return parsed


def parse_planner_output(result: Any) -> PlannerOutput:
    if isinstance(result, PlannerOutput):
        return result
    if isinstance(result, dict):
        return PlannerOutput.model_validate(result)
    if hasattr(result, "tasks"):
        return PlannerOutput.model_validate(result)
    if hasattr(result, "content"):
        content = _message_content_text(result.content)
        return PlannerOutput.model_validate(_extract_json_object(content))
    return PlannerOutput.model_validate(_extract_json_object(str(result)))


async def planner_node(state: AgentState, config: RunnableConfig) -> dict:
    llm = config.get("configurable", {}).get("planner_llm")
    if llm is None:
        return {"route": "chitchat", "sub_task": "Error: planner LLM not configured.",
                "pending_routes": [], "original_query": "", "had_multiple_tasks": False}
    messages = state.get("messages", [])

    recent = [
        m for m in messages
        if hasattr(m, "type") and m.type in ("user", "assistant")
    ][-PLANNER_WINDOW_SIZE:]

    if not recent:
        recent = messages[-2:] if len(messages) >= 2 else messages

    cfg = config.get("configurable", {})
    conv_ctx = cfg.get("conversation_context", "")
    today = cfg.get("today", "")

    planner_system = PLANNER_PROMPT
    if today:
        planner_system += f"\n\nToday's date: {today}"
    if conv_ctx:
        planner_system += f"\n\n{conv_ctx}"
    if cfg.get("planner_output_mode") == "json_text":
        planner_system += f"\n\n{PLANNER_JSON_INSTRUCTION}"

    result = await llm.ainvoke([SystemMessage(content=planner_system)] + recent)
    try:
        planner_output = parse_planner_output(result)
        tasks = planner_output.tasks
    except (json.JSONDecodeError, ValueError):
        tasks = []

    valid = {"transaction", "analytics", "ingestion", "chitchat"}
    filtered = []
    for t in tasks:
        if t.route.lower() in valid:
            filtered.append({"route": t.route.lower(), "task": t.task})

    if not filtered:
        filtered = [{"route": "chitchat", "task": "Answer this general question helpfully."}]

    original_query = ""
    for m in reversed(messages):
        if hasattr(m, "type") and m.type == "human":
            original_query = m.content
            break

    pending = [f for f in filtered]
    first = pending.pop(0)
    return {
        "route": first["route"],
        "sub_task": first["task"],
        "pending_routes": pending,
        "original_query": original_query,
        "had_multiple_tasks": len(filtered) > 1,
    }


def route_condition(state: AgentState) -> str:
    return state.get("route", "chitchat")


def merge_node(state: AgentState) -> dict:
    """Pop the next pending route and sub_task, or signal end."""
    messages = state.get("messages", [])

    if messages:
        last = messages[-1]
        content = getattr(last, "content", "") or ""
        if isinstance(content, str) and '"status": "PREVIEW"' in content:
            return {"pending_routes": [], "route": "", "sub_task": ""}

    pending = list(state.get("pending_routes", []))
    if not pending:
        return {"pending_routes": [], "route": "", "sub_task": ""}

    next_item = pending.pop(0)
    return {
        "route": next_item["route"],
        "sub_task": next_item["task"],
        "pending_routes": pending,
    }


def merge_condition(state: AgentState) -> str:
    route = state.get("route", "")
    if route:
        return route
    if state.get("had_multiple_tasks"):
        return "synthesizer"
    return END  # type: ignore[return-value]
