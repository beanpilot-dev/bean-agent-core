"""PersonalFinanceAgent — single-loop LangGraph runtime and SSE streaming."""

import asyncio
import json
import logging
import os
import time
from typing import Any, AsyncGenerator
from urllib.parse import urlparse

from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import message_chunk_to_message
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from agent_core.services.activity import ActivityCallbackHandler, ActivityEmitter
from agent_core.services.tool_ports import (
    WorkflowToolDependenciesFactory,
    create_workflow_tool_dependencies,
)
from agent_core.services.types import LedgerConfig
from agent_core.services.workspace import GitService
from agent_core.tracing import get_tracing_manager
from agent_core.workflow.language import detect_preferred_language, response_language_instruction
from agent_core.workflow.state import AgentState
from agent_core.workflow.tools import MODEL_TOOLS

logger = logging.getLogger(__name__)

_PROMPT_FILE = os.path.join(os.path.dirname(__file__), "ledger", "prompt.md")
SYSTEM_PROMPT = open(_PROMPT_FILE).read()


def _serialize_history(messages) -> list[dict]:
    role_map = {"human": "user", "ai": "assistant", "system": "system"}
    return [
        {
            "role": role_map.get(getattr(m, "type", "user"), "user"),
            "content": getattr(m, "content", ""),
        }
        for m in messages
    ]


def _has_pending_action_status(payload: Any) -> bool:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return False
    return isinstance(payload, dict) and payload.get("status") in {
        "PENDING_ACTION",
        "approval_required",
    }


def is_deepseek_thinking_model(model: str) -> bool:
    return model in {"deepseek-v4-pro", "deepseek-v4-flash"}


def supports_stream_usage(base_url: str | None) -> bool:
    """Return whether the configured provider supports usage in streamed chunks."""
    if not base_url:
        return True
    hostname = urlparse(base_url).hostname
    return hostname == "api.deepseek.com" or bool(
        hostname and hostname.endswith(".deepseek.com")
    )


def message_token_count(message: Any) -> int:
    """Read a message's token total across LangChain metadata formats."""
    usage_metadata = getattr(message, "usage_metadata", None)
    if isinstance(usage_metadata, dict):
        total_tokens = usage_metadata.get("total_tokens")
        if isinstance(total_tokens, (int, float)) and total_tokens >= 0:
            return int(total_tokens)

    response_metadata = getattr(message, "response_metadata", None)
    if not isinstance(response_metadata, dict):
        return 0

    for usage_key in ("token_usage", "usage"):
        usage = response_metadata.get(usage_key)
        if not isinstance(usage, dict):
            continue
        total_tokens = usage.get("total_tokens")
        if isinstance(total_tokens, (int, float)) and total_tokens >= 0:
            return int(total_tokens)
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        if (
            isinstance(input_tokens, (int, float))
            and input_tokens >= 0
            and isinstance(output_tokens, (int, float))
            and output_tokens >= 0
        ):
            return int(input_tokens + output_tokens)
    return 0


def validate_model_name(model: str) -> str:
    normalized = model.strip()
    if not normalized:
        raise ValueError("model must not be empty")
    if normalized != model or any(ch.isspace() for ch in normalized):
        raise ValueError("model must not contain whitespace")
    if normalized.endswith(".env"):
        raise ValueError("model appears to be an environment file name")
    return normalized


def normalize_conversation_title(raw_title: str) -> str:
    title = " ".join(raw_title.strip().split())
    title = title.strip("\"'`*_#[]() ")
    title = title.rstrip(".。!！?？:：;；,，")
    words = title.split()
    if len(words) > 8:
        title = " ".join(words[:8])
    if len(title) > 48:
        title = title[:48].rstrip()
    if not title or "\n" in title or "|" in title:
        return ""
    return title


def _format_ledger_context(ledger_context: dict[str, Any] | None) -> str:
    if not isinstance(ledger_context, dict):
        return ""
    compact_context: dict[str, Any] = {}
    for key in (
        "status",
        "target",
        "ledger_meta",
        "balance_snapshot",
        "flow_summary",
        "recent_activity",
        "recent_ledger_text",
        "context_truncated",
        "accounts_truncated",
        "accounts_omitted",
        "errors",
        "errors_truncated",
    ):
        if key in ledger_context and ledger_context[key] is not None:
            value = ledger_context[key]
            if key == "errors" and isinstance(value, str) and len(value) > 2_000:
                compact_context[key] = value[-2_000:]
                compact_context["errors_truncated"] = True
            else:
                compact_context[key] = value

    # The grouped representation is canonical in the prompt. Keep support for
    # older callers that still provide only the flat account list.
    grouped_accounts = ledger_context.get("accounts_by_type")
    if grouped_accounts is None:
        grouped_accounts = ledger_context.get("accounts")
    if grouped_accounts is not None:
        compact_context["accounts"] = grouped_accounts

    if "recent_activity" not in compact_context and "recent_ledger_text" not in compact_context:
        if "recent" in ledger_context and ledger_context["recent"] is not None:
            compact_context["recent"] = ledger_context["recent"]

    if not compact_context:
        return ""
    return "\nLEDGER CONTEXT:\n" + json.dumps(
        compact_context, ensure_ascii=False, separators=(",", ":")
    ) + "\n"


def _build_single_loop_prompt(
    conversation_context: str,
    ledger_context: dict[str, Any] | None,
    preferred_language: str,
) -> str:
    return (
        SYSTEM_PROMPT
        + _format_ledger_context(ledger_context)
        + (f"\nCONVERSATION CONTEXT:\n{conversation_context}\n" if conversation_context else "")
        + "\nRESPONSE LANGUAGE:\n"
        + response_language_instruction(preferred_language)
    )


async def _single_agent_node(state: AgentState, config: RunnableConfig) -> dict:
    llm = config.get("configurable", {}).get("single_loop_llm")
    if llm is None:
        return {"messages": []}
    cfg = config.get("configurable", {})
    conversation_context = cfg.get("conversation_context", "")
    ledger_context = cfg.get("ledger_context")
    preferred_language = state.get("preferred_language", "auto")
    prompt = _build_single_loop_prompt(
        conversation_context=conversation_context,
        ledger_context=ledger_context,
        preferred_language=preferred_language,
    )
    messages = list(state["messages"])
    if messages and isinstance(messages[0], SystemMessage):
        messages[0] = SystemMessage(content=prompt)
    else:
        messages.insert(0, SystemMessage(content=prompt))
    content_stream_queue = cfg.get("content_stream_queue")
    if not hasattr(llm, "astream"):
        response = await llm.ainvoke(messages, config=config)
        return {"messages": [response], "route": "single_loop"}

    response_chunk = None
    async for chunk in llm.astream(messages, config=config):
        response_chunk = chunk if response_chunk is None else response_chunk + chunk
        if isinstance(content_stream_queue, asyncio.Queue):
            text = _message_content_to_text(getattr(chunk, "content", ""))
            if text:
                await content_stream_queue.put(text)
    response = (
        message_chunk_to_message(response_chunk)
        if response_chunk is not None
        else await llm.ainvoke(messages, config=config)
    )
    return {"messages": [response], "route": "single_loop"}


def _message_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "".join(parts)


def _single_loop_condition(state: AgentState) -> str:
    messages = state.get("messages", [])
    if not messages:
        return END
    tool_calls = getattr(messages[-1], "tool_calls", None)
    return "tools" if tool_calls else END


async def generate_conversation_title(
    query: str,
    api_key: str | None = None,
    model: str = "gpt-4o",
) -> str:
    model = validate_model_name(model)
    llm_kwargs: dict[str, Any] = {
        "model": model,
        "api_key": api_key or "none",
        "temperature": 0,
    }
    if os.environ.get("OPENAI_BASE_URL"):
        llm_kwargs["base_url"] = os.environ["OPENAI_BASE_URL"]
    llm = ChatOpenAI(**llm_kwargs)
    response = await llm.ainvoke(
        [
            SystemMessage(
                content=(
                    "Generate a concise conversation title from the user's first message. "
                    "Return only the title as plain text. No markdown, quotes, labels, "
                    "or punctuation. "
                    "Use at most 8 words and avoid exposing unnecessary sensitive detail."
                )
            ),
            HumanMessage(content=query),
        ]
    )
    return normalize_conversation_title(str(response.content))


# ── PersonalFinanceAgent ──────────────────────────────────────────────────────


class PersonalFinanceAgent:
    def __init__(
        self,
        tool_dependencies_factory: WorkflowToolDependenciesFactory = (
            create_workflow_tool_dependencies
        ),
    ):
        self._tool_dependencies_factory = tool_dependencies_factory
        self.graph = self._build_graph()
        self.model_tools = MODEL_TOOLS

    def _build_graph(self):
        builder = StateGraph(AgentState)
        builder.add_node("agent", _single_agent_node)
        builder.add_node("tools", ToolNode(MODEL_TOOLS))
        builder.add_edge(START, "agent")
        builder.add_conditional_edges(
            "agent",
            _single_loop_condition,
            {
                "tools": "tools",
                END: END,
            },
        )
        builder.add_edge("tools", "agent")
        return builder.compile()

    @staticmethod
    def _requires_user_input(result: dict) -> bool:
        messages = result.get("messages", [])

        for msg in messages:
            if not isinstance(msg, ToolMessage):
                continue

            content = getattr(msg, "content", "") or ""
            if _has_pending_action_status(content):
                return True
            if isinstance(content, list):
                for part in content:
                    if _has_pending_action_status(part):
                        return True
                    if isinstance(part, dict) and _has_pending_action_status(part.get("text")):
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
        ledger_config: LedgerConfig | None = None,
        ledger_context: dict[str, Any] | None = None,
        activity_emitter: ActivityEmitter | None = None,
    ) -> AsyncGenerator[dict, None]:
        yield {"is_task_complete": False, "require_user_input": False, "content": ""}

        start_time = time.monotonic()
        tracing = get_tracing_manager()
        conversation_id = conversation_meta.get("id") if conversation_meta else None

        trace_metadata = {
            "conversation_id": conversation_id,
            "conversation_name": conversation_meta.get("name") if conversation_meta else None,
            "conversation_tag": conversation_meta.get("tag") if conversation_meta else None,
        }

        try:
            model = validate_model_name(model)
        except ValueError as e:
            raise ValueError(f"Invalid model configuration: {e}") from e

        base_llm_kwargs: dict[str, Any] = {
            "model": model,
            "api_key": api_key or "none",
        }
        base_url = os.environ.get("OPENAI_BASE_URL")
        if base_url:
            base_llm_kwargs["base_url"] = base_url
        if supports_stream_usage(base_url):
            base_llm_kwargs["stream_usage"] = True

        base_llm = ChatOpenAI(**base_llm_kwargs)
        tool_dependencies = self._tool_dependencies_factory()

        try:
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

            preferred_language = detect_preferred_language(query)
            system = SystemMessage(
                content=_build_single_loop_prompt(
                    conversation_context=conv_ctx,
                    ledger_context=ledger_context,
                    preferred_language=preferred_language,
                )
            )
            messages = [system] + prior + [HumanMessage(content=query)]

            if activity_emitter:
                yield activity_emitter.emit(
                    category="workflow",
                    state="started",
                    phase="prepare",
                    actor="agent",
                    visibility="timeline",
                    display_key="workflow.prepare.started",
                    fallback_text="Preparing a response",
                )

            with tracing.trace(
                task="agent-turn", input=query, **trace_metadata
            ) as handler:
                activity_queue: asyncio.Queue[dict[str, Any]] | None = (
                    asyncio.Queue() if activity_emitter else None
                )
                content_stream_queue: asyncio.Queue[str] = asyncio.Queue()
                callbacks = []
                if tracing.enabled:
                    callbacks.append(handler)
                if activity_emitter and activity_queue:
                    callbacks.append(ActivityCallbackHandler(activity_emitter, activity_queue))
                config: RunnableConfig = {
                    "callbacks": callbacks,
                    "configurable": {
                        "single_loop_llm": base_llm.bind_tools(MODEL_TOOLS),
                        "router_system_prompt": SYSTEM_PROMPT,
                        "conversation_context": conv_ctx,
                        "workspace": workspace,
                        "repo_url": repo_url,
                        "token": token,
                        "git_service": git_service,
                        "whitelist": whitelist,
                        "ledger_config": ledger_config,
                        "ledger_context": ledger_context,
                        "tool_dependencies": tool_dependencies,
                        "content_stream_queue": content_stream_queue,
                    },
                }
                if activity_emitter:
                    yield activity_emitter.emit(
                        category="workflow",
                        state="started",
                        phase="execution",
                        actor="agent",
                        visibility="details",
                        display_key="workflow.execution.started",
                        fallback_text="Running the agent loop",
                    )
                graph_input = {  # pyright: ignore[reportAssignmentType]
                    "messages": messages,
                    "route": "single_loop",
                    "sub_task": "",
                    "task_id": "",
                    "original_query": query if isinstance(query, str) else "",
                    "pending_routes": [],
                    "planned_tasks": [],
                    "had_multiple_tasks": False,
                    "preferred_language": preferred_language,
                }
                graph_task = asyncio.create_task(self.graph.ainvoke(graph_input, config=config))
                while not graph_task.done():
                    yielded = False
                    while not content_stream_queue.empty():
                        yielded = True
                        yield {
                            "is_task_complete": False,
                            "require_user_input": False,
                            "content": content_stream_queue.get_nowait(),
                        }
                    if activity_queue:
                        while not activity_queue.empty():
                            yielded = True
                            yield activity_queue.get_nowait()
                    if not yielded:
                        await asyncio.wait({graph_task}, timeout=0.05)
                try:
                    result = await graph_task
                except Exception:
                    if activity_queue:
                        while not activity_queue.empty():
                            yield activity_queue.get_nowait()
                    raise
                while not content_stream_queue.empty():
                    yield {
                        "is_task_complete": False,
                        "require_user_input": False,
                        "content": content_stream_queue.get_nowait(),
                    }
                if activity_queue:
                    while not activity_queue.empty():
                        yield activity_queue.get_nowait()

                response = result["messages"][-1].content
                tracing.update_root_observation(output=response)

            require_input = self._requires_user_input(result)
            tool_names = _tool_names(result)
            pending_actions = _pending_actions(result)
            if activity_emitter:
                yield activity_emitter.emit(
                    category="workflow",
                    state="completed",
                    phase="prepare" if require_input else "execution",
                    actor="agent",
                    visibility="timeline",
                    display_key=(
                        "workflow.awaiting_approval"
                        if require_input
                        else "workflow.execution.completed"
                    ),
                    fallback_text=("Preview ready" if require_input else "Agent loop completed"),
                    display_args={
                        "tool_count": len(tool_names),
                    },
                )
                yield activity_emitter.emit(
                    category="workflow",
                    state="completed",
                    phase="execution",
                    actor="agent",
                    visibility="details",
                    display_key="workflow.loop.completed",
                    fallback_text="Agent loop completed",
                    display_args={"tool_count": len(tool_names)},
                )
                if tool_names:
                    yield activity_emitter.emit(
                        category="tool",
                        state="completed",
                        phase="execution",
                        actor="agent",
                        visibility="details",
                        display_key="agent.tools.completed",
                        fallback_text="Ledger tools completed",
                        display_args={"tool_count": len(tool_names)},
                    )

            updated_history = _serialize_history(result["messages"][1:])

            trace_id = tracing.get_trace_id()
            trace_url = tracing.get_trace_url()

            total_tokens = 0
            for msg in result["messages"]:
                total_tokens += message_token_count(msg)

            duration_ms = int((time.monotonic() - start_time) * 1000)

            yield {
                "is_task_complete": not require_input,
                "require_user_input": require_input,
                "content": response,
            }
            for pending_action in pending_actions:
                yield {
                    "type": "approval_required",
                    "is_task_complete": True,
                    "require_user_input": True,
                    "pending_action": pending_action,
                    "content": pending_action.get("message", ""),
                }
            yield {
                "type": "history_snapshot",
                "messages": updated_history,
                "trace_id": trace_id,
                "trace_url": trace_url,
                "usage": {"tokens": total_tokens, "duration_ms": duration_ms},
            }
        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.exception(
                "Agent error conversation_id=%s model=%s duration_ms=%d",
                conversation_id,
                model,
                duration_ms,
            )
            if activity_emitter:
                yield activity_emitter.emit(
                    category="node",
                    state="failed",
                    phase="execution",
                    actor="agent",
                    visibility="timeline",
                    display_key="agent.execution.failed",
                    fallback_text="Agent workflow failed",
                    safe_detail_summary=type(e).__name__,
                )
            yield {
                "is_task_complete": True,
                "require_user_input": False,
                "content": "Agent request failed. Please try again.",
            }
            yield {
                "type": "history_snapshot",
                "messages": (
                    prior
                    if prior and all(isinstance(m, dict) for m in prior)
                    else _serialize_history(prior)
                ),
                "trace_id": tracing.get_trace_id() if tracing else None,
                "trace_url": tracing.get_trace_url() if tracing else None,
                "usage": {"tokens": 0, "duration_ms": duration_ms},
            }


def _tool_names(result: dict) -> list[str]:
    names: list[str] = []
    for msg in result.get("messages", []):
        if not isinstance(msg, ToolMessage):
            continue
        name = getattr(msg, "name", None)
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _pending_actions(result: dict) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for msg in result.get("messages", []):
        if not isinstance(msg, ToolMessage):
            continue
        content = getattr(msg, "content", "") or ""
        if isinstance(content, str):
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("status") == "PENDING_ACTION":
                actions.append(payload)
            elif payload.get("status") == "approval_required":
                pending_action = payload.get("pending_action")
                if isinstance(pending_action, dict):
                    actions.append(pending_action)
    return actions
