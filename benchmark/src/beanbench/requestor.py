"""HTTP client for agent-core /agent/chat endpoint with SSE parsing."""

import json
import re
from typing import Any

import httpx

BENCHMARK_USER_ID = "beanbench"


def _build_chat_payload(
    query: str,
    prior_messages: list[dict],
    model: str,
    api_key: str,
    case_id: str,
) -> dict[str, Any]:
    return {
        "repo": {"url": "placeholder", "token": "placeholder"},
        "user_id": BENCHMARK_USER_ID,
        "request_id": f"bench-{case_id}",
        "api_key": api_key,
        "model": model,
        "query": query,
        "conversation": {"id": case_id},
        "messages": prior_messages or [],
        "ledger": {
            "entry_path": "main.beancount",
            "sidecar_main_path": "data/agent_inc/main.beancount",
            "sidecar_write_dir": "data/agent_inc",
        },
    }


async def send_chat_request(
    query: str,
    prior_messages: list[dict],
    model: str,
    api_key: str,
    case_id: str,
    port: int,
    timeout: int = 120,
) -> dict[str, Any]:
    """Send one turn to agent-core, parse the SSE stream.

    Returns dict with keys:
      - response_text: str (full accumulated content)
      - require_user_input: bool
      - is_task_complete: bool
      - history_messages: list[dict] (snapshot from agent)
      - usage: dict | None
      - trace_id: str | None
      - trace_url: str | None
      - error: str | None (fatal error message)
    """
    payload = _build_chat_payload(query, prior_messages, model, api_key, case_id)
    result: dict[str, Any] = {
        "response_text": "",
        "require_user_input": False,
        "is_task_complete": False,
        "history_messages": [],
        "usage": None,
        "trace_id": None,
        "trace_url": None,
        "error": None,
    }

    base_content_parts: list[str] = []

    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST",
            f"http://127.0.0.1:{port}/agent/chat",
            json=payload,
            timeout=timeout,
        ) as response:
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]

                if data_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                if chunk.get("type") == "fatal":
                    result["error"] = chunk.get("message", "Unknown fatal error")
                    break

                if chunk.get("type") == "history_snapshot":
                    result["history_messages"] = chunk.get("messages", [])
                    result["usage"] = chunk.get("usage")
                    result["trace_id"] = chunk.get("trace_id")
                    result["trace_url"] = chunk.get("trace_url")

                if chunk.get("type") == "activity":
                    continue

                content = chunk.get("content", "")
                if content:
                    base_content_parts.append(content)

                result["require_user_input"] = chunk.get("require_user_input", False)
                result["is_task_complete"] = chunk.get("is_task_complete", False)

    result["response_text"] = "".join(base_content_parts)
    return result


async def run_single_turn(
    query: str,
    prior_messages: list[dict],
    model: str,
    api_key: str,
    case_id: str,
    port: int,
    timeout: int = 120,
) -> dict[str, Any]:
    """Convenience wrapper for a single-turn request."""
    return await send_chat_request(query, prior_messages, model, api_key, case_id, port, timeout)


async def run_multi_turn(
    turns: list[dict],
    model: str,
    api_key: str,
    case_id: str,
    port: int,
    timeout: int = 120,
) -> dict[str, Any]:
    """Execute multiple turns, accumulating message history between calls."""
    messages: list[dict] = []
    last_resp: dict[str, Any] = {}
    turn_traces: list[dict[str, Any]] = []
    for i, turn in enumerate(turns):
        user_content = turn.get("content", "")
        resp = await send_chat_request(
            query=user_content,
            prior_messages=messages,
            model=model,
            api_key=api_key,
            case_id=f"{case_id}-turn{i}",
            port=port,
            timeout=timeout,
        )
        turn_traces.append({
            "turn": i + 1,
            "trace_id": resp.get("trace_id"),
            "trace_url": resp.get("trace_url"),
        })
        if resp.get("error"):
            resp["turn_traces"] = turn_traces
            return resp
        messages = resp.get("history_messages", [])
        last_resp = resp
    last_resp["turn_traces"] = turn_traces
    return last_resp


BEANCOUNT_BLOCK_RE = re.compile(
    r"```(?:beancount)?\s*\n(.*?)```", re.DOTALL
)


def extract_beancount_block(text: str) -> str | None:
    """Extract the first Beancount code block from agent response text."""
    match = BEANCOUNT_BLOCK_RE.search(text)
    if match:
        return match.group(1).strip()
    return None
