"""Integration tests across API, orchestrator, workflow tools, and services."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from agent_core.services.orchestrator import AgentOrchestrator
from agent_core.services.workspace import CachedWorkspaceManager, LocalGitService
from agent_core.workflow.tools import tool_account_balance, tool_query_template


def test_query_template_tool_works_directly(ledger_workspace: Path) -> None:
    result = json.loads(
        tool_query_template.invoke(
            {
                "template_name": "account_snapshot",
                "params": {"account_pattern": "^Assets"},
            },
            config={"configurable": {"workspace": str(ledger_workspace)}},
        )
    )

    assert result["status"] == "SUCCESS"
    assert result["template"] == "account_snapshot"


def test_account_balance_tool_works_directly(ledger_workspace: Path) -> None:
    result = json.loads(
        tool_account_balance.invoke(
            {"account": "Assets:Cash"},
            config={"configurable": {"workspace": str(ledger_workspace)}},
        )
    )

    assert result["status"] == "SUCCESS"
    assert result["account"] == "Assets:Cash"
    assert "CNY" in result["balance"]


class WorkflowAgent:
    def __init__(self):
        async def query_node(_state, config):
            result = json.loads(
                tool_account_balance.invoke(
                    {"account": "Assets:Cash"},
                    config=config,
                )
            )
            return {
                "messages": [
                    AIMessage(content=f"Assets:Cash balance is {result['balance']}")
                ]
            }

        builder = StateGraph(MessagesState)
        builder.add_node("analytics_workflow", query_node)
        builder.add_edge(START, "analytics_workflow")
        builder.add_edge("analytics_workflow", END)
        self.graph = builder.compile()

    async def stream(self, *, query: str, workspace: str, **_kwargs):
        result = await self.graph.ainvoke(
            {"messages": [HumanMessage(content=query)]},
            config={"configurable": {"workspace": workspace}},
        )
        yield {
            "is_task_complete": True,
            "require_user_input": False,
            "content": result["messages"][-1].content,
        }
        yield {"type": "history_snapshot", "messages": []}


@pytest.mark.asyncio
async def test_full_api_service_workflow_tool_response_flow(
    tmp_path: Path, bare_ledger_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_core import main

    git = LocalGitService(str(bare_ledger_repo))
    cache = CachedWorkspaceManager(git, ttl_seconds=-1)
    monkeypatch.setattr(cache, "CACHE_ROOT", str(tmp_path / "cache"))
    monkeypatch.setattr(
        main,
        "_orchestrator",
        AgentOrchestrator(WorkflowAgent(), cache, git),
    )
    response = await main.agent_chat(
        main.ChatRequest(
            repo=main.RepoInfo(url="ignored", token="ignored"),
            user_id="user",
            request_id="request",
            api_key="key",
            model="scripted",
            query="What is my cash balance?",
            conversation=main.ChatConversationMeta(),
            messages=[],
        )
    )
    chunks = [
        chunk.decode() if isinstance(chunk, bytes) else chunk
        async for chunk in response.body_iterator
    ]
    body = "".join(chunks)

    assert "Assets:Cash balance is" in body
    assert "CNY" in body
    assert "history_snapshot" in body


@pytest.mark.asyncio
async def test_stats_and_accounts_run_through_services(
    tmp_path: Path, bare_ledger_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_core.services import orchestrator as orchestrator_module
    from agent_core.services.workspace import CachedWorkspaceManager

    git = LocalGitService(str(bare_ledger_repo))
    cache = CachedWorkspaceManager(git, ttl_seconds=-1)
    monkeypatch.setattr(cache, "CACHE_ROOT", str(tmp_path / "cache"))
    snapshots = iter([tmp_path / "bean_stats_test", tmp_path / "bean_accounts_test"])
    monkeypatch.setattr(
        orchestrator_module.tempfile,
        "mkdtemp",
        lambda **_kwargs: str(next(snapshots)),
    )
    orchestrator = AgentOrchestrator(Mock(), cache, git)

    stats = await orchestrator.run_stats(
        repo_url="ignored",
        token=None,
        user_id="user",
        request_id="stats",
        tag="#trip",
    )
    accounts = await orchestrator.run_accounts(
        repo_url="ignored",
        token=None,
        user_id="user",
        request_id="accounts",
    )

    assert stats["status"] == "ok"
    assert stats["rows"] == []
    assert accounts["status"] == "ok"
    assert "Assets:Cash" in accounts["accounts"]
    assert not (tmp_path / "bean_stats_test").exists()
    assert not (tmp_path / "bean_accounts_test").exists()


@pytest.mark.asyncio
async def test_stats_and_accounts_json_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core import main

    monkeypatch.setattr(
        main._orchestrator,
        "run_stats",
        AsyncMock(return_value={"status": "ok", "rows": [{"account": "Expenses:Food"}]}),
    )
    monkeypatch.setattr(
        main._orchestrator,
        "run_accounts",
        AsyncMock(
            return_value={
                "status": "ok",
                "accounts": ["Assets:Cash"],
                "raw_accounts": ["2020-01-01 open Assets:Cash CNY"],
            }
        ),
    )
    common = {
        "repo": {"url": "ignored", "token": "ignored"},
        "user_id": "user",
        "request_id": "request",
    }

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        stats = await client.post(
            "/agent/stats",
            json={**common, "conversation": {"tag": "#trip"}},
        )
        accounts = await client.post("/agent/accounts", json=common)

    assert stats.status_code == 200
    assert stats.json()["rows"] == [{"account": "Expenses:Food"}]
    assert accounts.status_code == 200
    assert accounts.json()["accounts"] == ["Assets:Cash"]
