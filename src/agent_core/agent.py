"""PersonalFinanceAgent — LangGraph graph wiring and SSE streaming.

The agent uses a Planner → Pillar → Synthesizer architecture:

  START → planner (decompose into sub-tasks)
    → conditional_edge → first pillar (sees only its sub-task)
    → merge (pop next; stop if PREVIEW)
    → conditional_edge → next pillar → merge → ...
    → synthesizer (compose final response) → END

or for single-task / PREVIEW:
  START → planner → pillar → merge → END

Tool definitions, persona prompts, and sub-graph builders live in the
workflow/ module. This module owns only graph assembly and streaming.
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, AsyncGenerator

from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from agent_core.services.workspace import GitService
from agent_core.tracing import get_tracing_manager
from agent_core.workflow import (
    ANALYTICS_TOOLS,
    INGESTION_TOOLS,
    TRANSACTION_TOOLS,
    PlannerOutput,
    build_analytics_graph,
    build_chitchat_graph,
    build_ingestion_graph,
    build_transaction_graph,
    merge_condition,
    merge_node,
    planner_node,
    route_condition,
)
from agent_core.workflow.state import AgentState

logger = logging.getLogger(__name__)

_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "ledger", "prompt.md")
SYSTEM_PROMPT = open(_PROMPT_FILE).read()

SYNTHESIZER_PROMPT = """You are a response synthesizer. The conversation below contains the user's
original request and responses from one or more specialist workers who each handled
a part of the request. Produce a single, coherent, concise response that addresses
ALL parts of the user's request in a natural conversational tone.

Do NOT re-execute any tools — the specialists have already
handled that. Just weave their findings into one unified reply."""


# ── Pillar + synth routing map ────────────────────────────────────────────────

_PILLAR_MAP = {  # type: ignore[var-annotated]
    "transaction": "transaction",
    "analytics": "analytics",
    "ingestion": "ingestion",
    "chitchat": "chitchat",
    "synthesizer": "synthesizer",
    END: END,
}


def _serialize_history(messages) -> list[dict]:
    role_map = {"human": "user", "ai": "assistant", "system": "system"}
    return [
        {"role": role_map.get(getattr(m, "type", "user"), "user"),
         "content": getattr(m, "content", "")}
        for m in messages
    ]


def _has_preview_status(payload: Any) -> bool:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return False
    return isinstance(payload, dict) and payload.get("status") == "PREVIEW"


# ── PersonalFinanceAgent ──────────────────────────────────────────────────────


class PersonalFinanceAgent:

    def __init__(self):
        self._transaction_graph = build_transaction_graph()
        self._analytics_graph = build_analytics_graph()
        self._ingestion_graph = build_ingestion_graph()
        self._chitchat_graph = build_chitchat_graph()
        self.graph = self._build_graph()

    def _build_graph(self):
        builder = StateGraph(AgentState)
        builder.add_node("planner", planner_node)
        builder.add_node("transaction", self._transaction_graph)
        builder.add_node("analytics", self._analytics_graph)
        builder.add_node("ingestion", self._ingestion_graph)
        builder.add_node("chitchat", self._chitchat_graph)
        builder.add_node("merge", merge_node)
        builder.add_node("synthesizer", _synthesizer_node)
        builder.add_edge(START, "planner")
        builder.add_conditional_edges("planner", route_condition, _PILLAR_MAP)  # pyright: ignore[reportArgumentType]
        builder.add_edge("transaction", "merge")
        builder.add_edge("analytics", "merge")
        builder.add_edge("ingestion", "merge")
        builder.add_edge("chitchat", "merge")
        builder.add_conditional_edges("merge", merge_condition, _PILLAR_MAP)  # pyright: ignore[reportArgumentType]
        builder.add_edge("synthesizer", END)
        return builder.compile()

    @staticmethod
    def _requires_user_input(result: dict) -> bool:
        messages = result.get("messages", [])

        for msg in messages:
            if not isinstance(msg, ToolMessage):
                continue

            content = getattr(msg, "content", "") or ""
            if _has_preview_status(content):
                return True
            if isinstance(content, list):
                for part in content:
                    if _has_preview_status(part):
                        return True
                    if isinstance(part, dict) and _has_preview_status(part.get("text")):
                        return True

        return False

    async def stream(
        self,
        query: str | list,
        prior: list,
        conversation_meta: dict | None = None,
        api_key: str | None = None,
        model: str = "gpt-4o",
        workspace: str = "",
        repo_url: str = "",
        token: str | None = None,
        git_service: GitService | None = None,
        whitelist: list[str] | None = None,
    ) -> AsyncGenerator[dict, None]:
        yield {"is_task_complete": False, "require_user_input": False, "content": "Processing..."}

        start_time = time.monotonic()
        tracing = get_tracing_manager()
        conversation_id = conversation_meta.get("id") if conversation_meta else None

        trace_metadata = {
            "conversation_id": conversation_id,
            "conversation_name": conversation_meta.get("name") if conversation_meta else None,
            "conversation_tag": conversation_meta.get("tag") if conversation_meta else None,
        }

        base_llm_kwargs: dict[str, Any] = {
            "model": model,
            "api_key": api_key or "none",
        }
        if os.environ.get("OPENAI_BASE_URL"):
            base_llm_kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]

        base_llm = ChatOpenAI(**base_llm_kwargs)

        try:
            today = datetime.now().strftime("%Y-%m-%d")
            conv_ctx = ""
            if conversation_meta:
                parts = []
                if conversation_meta.get("tag"):
                    parts.append(
                        f"Conversation tag: {conversation_meta['tag']} — "
                        "append this tag to EVERY transaction you record."
                    )
                if conversation_meta.get("account_whitelist"):
                    parts.append(
                        f"Account whitelist: "
                        f"{', '.join(conversation_meta['account_whitelist'])} — "
                        "restrict account selection to these prefixes only."
                    )
                if parts:
                    conv_ctx = "\n".join(parts)

            system = SystemMessage(content=SYSTEM_PROMPT)
            messages = [system] + prior + [HumanMessage(content=query)]

            with tracing.trace(task="agent-turn", **trace_metadata) as handler:
                config: RunnableConfig = {
                    "callbacks": [handler] if tracing.enabled else [],
                    "configurable": {
                        "clerk_llm": base_llm.bind_tools(TRANSACTION_TOOLS),
                        "analyst_llm": base_llm.bind_tools(ANALYTICS_TOOLS),
                        "engineer_llm": base_llm.bind_tools(INGESTION_TOOLS),
                        "qa_llm": ChatOpenAI(**base_llm_kwargs),
                        "planner_llm": ChatOpenAI(**base_llm_kwargs).with_structured_output(
                            PlannerOutput, method="function_calling"
                        ),
                        "synthesizer_llm": ChatOpenAI(**base_llm_kwargs),
                        "router_system_prompt": SYSTEM_PROMPT,
                        "today": today,
                        "conversation_context": conv_ctx,
                        "workspace": workspace,
                        "repo_url": repo_url,
                        "token": token,
                        "git_service": git_service,
                        "whitelist": whitelist,
                    },
                }
                result = await self.graph.ainvoke(
                    {  # pyright: ignore[reportArgumentType]
                        "messages": messages,
                        "route": "",
                        "sub_task": "",
                        "original_query": "",
                        "pending_routes": [],
                        "had_multiple_tasks": False,
                    },
                    config=config,
                )

            response = result["messages"][-1].content
            require_input = self._requires_user_input(result)

            had_multiple = result.get("had_multiple_tasks", False)
            if had_multiple and not require_input:
                original = result.get("original_query", query)
                final_msg = result["messages"][-1]
                updated_history = _serialize_history([
                    HumanMessage(content=original),
                    final_msg,
                ])
            else:
                updated_history = _serialize_history(result["messages"][1:])

            trace_id = tracing.get_trace_id()
            trace_url = tracing.get_trace_url()

            total_tokens = 0
            for msg in result["messages"]:
                rmeta = getattr(msg, "response_metadata", None)
                if isinstance(rmeta, dict):
                    tu = rmeta.get("token_usage", {})
                    total_tokens += tu.get("total_tokens", 0)

            duration_ms = int((time.monotonic() - start_time) * 1000)

            yield {
                "is_task_complete": not require_input,
                "require_user_input": require_input,
                "content": response,
            }
            yield {
                "type": "history_snapshot",
                "messages": updated_history,
                "trace_id": trace_id,
                "trace_url": trace_url,
                "usage": {"tokens": total_tokens, "duration_ms": duration_ms},
            }
        except Exception as e:
            logger.exception("Agent error")
            duration_ms = int((time.monotonic() - start_time) * 1000)
            yield {"is_task_complete": True, "require_user_input": False, "content": f"Error: {e}"}
            yield {
                "type": "history_snapshot",
                "messages": (
                    prior if prior and all(isinstance(m, dict) for m in prior)
                    else _serialize_history(prior)
                ),
                "trace_id": tracing.get_trace_id() if tracing else None,
                "trace_url": tracing.get_trace_url() if tracing else None,
                "usage": {"tokens": 0, "duration_ms": duration_ms},
            }


# ── Synthesizer node ──────────────────────────────────────────────────────────


async def _synthesizer_node(state: AgentState, config: RunnableConfig) -> dict:
    llm = config.get("configurable", {}).get("synthesizer_llm")
    if llm is None:
        return {"messages": []}
    messages = list(state["messages"])
    if messages and isinstance(messages[0], SystemMessage):
        messages[0] = SystemMessage(content=SYNTHESIZER_PROMPT)
    else:
        messages.insert(0, SystemMessage(content=SYNTHESIZER_PROMPT))
    response = await llm.ainvoke(messages)
    return {"messages": [response]}
