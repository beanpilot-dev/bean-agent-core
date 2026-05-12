"""Agent Core — stateless Beancount ledger agent HTTP server.

Two endpoints:
    GET  /health     → liveness check
    POST /agent/run  → SSE stream of agent responses

The agent accepts per-request credentials (repo URL + short-lived token) and
executes ledger operations in an ephemeral workspace. No persistent state.
"""

import json
import logging
import os
import shutil
import uuid

import click
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agent_core.agent import PersonalFinanceAgent
from agent_core.context import (
    agent_model,
    agent_repo_url,
    agent_token,
    agent_workspace,
    conv_whitelist,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Agent Core", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_agent = PersonalFinanceAgent()


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

class ConversationMeta(BaseModel):
    tag: str | None = None
    account_whitelist: list[str] | None = None


class AgentRunRequest(BaseModel):
    repo_url: str
    token: str
    query: str
    conversation: ConversationMeta = ConversationMeta()
    messages: list[dict] = []
    model: str = "gpt-4o"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/agent/run")
async def agent_run(req: AgentRunRequest):
    """Main SSE endpoint. Streams agent responses back to the caller."""

    workspace_path = f"/tmp/bean_workspace_{uuid.uuid4().hex[:12]}"

    async def event_stream():
        # ContextVar lifespan: set before stream, reset in finally.
        ws_token = agent_workspace.set(workspace_path)
        repo_token = agent_repo_url.set(req.repo_url)
        gh_token = agent_token.set(req.token)
        model_token = agent_model.set(req.model)
        wl_token = conv_whitelist.set(req.conversation.account_whitelist)

        try:
            async for chunk in _agent.stream(
                query=req.query,
                prior=req.messages,
                conversation_meta={
                    "name": "agent-run",
                    "tag": req.conversation.tag,
                    "workspace": workspace_path,
                },
            ):
                if chunk.get("type") == "history_snapshot":
                    # Send the snapshot to the caller so they can persist it
                    yield f"data: {json.dumps(chunk, default=str)}\n\n"
                else:
                    yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            conv_whitelist.reset(wl_token)
            agent_model.reset(model_token)
            agent_token.reset(gh_token)
            agent_repo_url.reset(repo_token)
            agent_workspace.reset(ws_token)
            shutil.rmtree(workspace_path, ignore_errors=True)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@click.command()
@click.option("--host", default=os.environ.get("AGENT_HOST", "0.0.0.0"))
@click.option("--port", default=int(os.environ.get("AGENT_PORT", "8000")), type=int)
def main(host: str, port: int):
    logger.info(f"Starting agent-core at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
