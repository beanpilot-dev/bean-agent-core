"""Agent Core — stateless Beancount ledger agent HTTP server.

Endpoints:
    GET  /health           → liveness check
    POST /agent/chat       → SSE stream of agent responses (LLM + tools)
    POST /agent/stats      → JSON, spending stats scoped by conversation tag
    POST /agent/accounts   → JSON, valid account prefixes from the ledger
    POST /agent/run        → DEPRECATED, forwards to /agent/chat

The agent accepts per-request credentials (repo URL + short-lived token) and
executes ledger operations in an ephemeral workspace. No persistent state.
"""

import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import click
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from agent_core.agent import PersonalFinanceAgent
from agent_core.context import (
    agent_api_key,
    agent_model,
    agent_repo_url,
    agent_request_id,
    agent_token,
    agent_user_id,
    agent_workspace,
    conv_whitelist,
)
from agent_core.ledger import _beancount as bc
from agent_core.ledger import state
from agent_core.ledger import workspace as ws

# Load environment from project root (agent-core/).  .env.local overrides .env.
_project_root = Path(__file__).resolve().parent.parent.parent
load_dotenv(_project_root / ".env")
load_dotenv(_project_root / ".env.local", override=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Agent Core", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_agent = PersonalFinanceAgent()


# ---------------------------------------------------------------------------
# Payload sanitization
# ---------------------------------------------------------------------------

def _mask_secret(value: str, prefix: str) -> str:
    """Mask a secret, keeping only the prefix and last 4 characters."""
    if not value:
        return value
    if len(value) <= len(prefix) + 4:
        return value[:len(prefix)] + "***"
    return f"{value[:len(prefix)]}***...***{value[-4:]}"


def sanitize_payload(body: dict | None) -> dict | None:
    """Return a copy of the request body with sensitive fields masked.

    Masks api_key (sk-***...***) and repo.token (ghs_***...***) fields.
    Does not mutate the original dict.
    """
    if body is None:
        return None
    sanitized = json.loads(json.dumps(body))  # deep copy via JSON round-trip
    if "api_key" in sanitized and isinstance(sanitized["api_key"], str):
        sanitized["api_key"] = _mask_secret(sanitized["api_key"], "sk-")
    if "repo" in sanitized and isinstance(sanitized["repo"], dict):
        token = sanitized["repo"].get("token")
        if isinstance(token, str):
            sanitized["repo"]["token"] = _mask_secret(token, "ghs_")
    return sanitized


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

def _error_envelope(
    code: str, message: str, status_code: int, details: dict | None = None
) -> JSONResponse:
    """Build a standard error JSON response envelope."""
    err: dict[str, Any] = {"code": code, "message": message}
    if details:
        err["details"] = details
    return JSONResponse(
        content={"status": "error", "error": err},
        status_code=status_code,
    )


def _repo_error_to_envelope(exc: Exception) -> JSONResponse:
    """Map common repository errors to standard error responses."""
    msg = str(exc).lower()
    if "not found" in msg or "remote:" in msg and "not found" in msg:
        return _error_envelope("REPO_UNREACHABLE", str(exc), 502)
    if "authentication" in msg or "auth" in msg or "401" in msg:
        return _error_envelope("REPO_AUTH_FAILED", str(exc), 401)
    return _error_envelope("REPO_UNREACHABLE", str(exc), 502)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class RepoInfo(BaseModel):
    url: str
    token: str | None = None


class ChatConversationMeta(BaseModel):
    id: str | None = None
    tag: str | None = None
    account_whitelist: list[str] | None = None


class StatsConversationMeta(BaseModel):
    tag: str


class ChatRequest(BaseModel):
    repo: RepoInfo
    user_id: str
    request_id: str | None = None
    api_key: str
    model: str = os.environ.get("OPENAI_MODEL", "gpt-4o")
    query: str
    conversation: ChatConversationMeta = ChatConversationMeta()
    messages: list[dict] = []


class StatsRequest(BaseModel):
    repo: RepoInfo
    user_id: str
    request_id: str | None = None
    conversation: StatsConversationMeta


class AccountsRequest(BaseModel):
    repo: RepoInfo
    user_id: str
    request_id: str | None = None


# ---------------------------------------------------------------------------
# Accounts helper
# ---------------------------------------------------------------------------

def _get_raw_open_directives(workspace_path: str) -> list[str]:
    """Return all open directives from ledger .beancount files."""
    directives: list[str] = []
    data_dir = os.path.join(workspace_path, "data")
    try:
        for dirpath, _dirnames, filenames in os.walk(data_dir):
            for fname in sorted(filenames):
                if not fname.endswith(".beancount"):
                    continue
                try:
                    with open(os.path.join(dirpath, fname)) as f:
                        for line in f:
                            if re.match(r"\d{4}-\d{2}-\d{2}\s+open\s+", line):
                                directives.append(line.strip())
                except OSError:
                    pass
    except OSError:
        pass
    return directives


# ---------------------------------------------------------------------------
# Exception handler — sanitize sensitive fields in error responses
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Log sanitized request body on any unhandled exception."""
    body_str = None
    try:
        raw_body = await request.body()
        if raw_body:
            body_dict = json.loads(raw_body)
            body_str = json.dumps(sanitize_payload(body_dict))
    except Exception:
        body_str = "<unparseable>"

    logger.error(
        "Unhandled exception for %s %s — body: %s",
        request.method,
        request.url.path,
        body_str,
        exc_info=True,
    )
    return _error_envelope("INTERNAL_ERROR", str(exc), 500)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "0.2.0",
        "beancount": "3.0.0",
    }


# ---------------------------------------------------------------------------
# POST /agent/chat — full agent loop, SSE streaming
# ---------------------------------------------------------------------------

@app.post("/agent/chat")
async def agent_chat(req: ChatRequest):
    workspace_path = f"/tmp/bean_workspace_{uuid.uuid4().hex[:12]}"

    logger.info(
        "agent-chat user_id=%s request_id=%s conv_id=%s sanitized_body=%s",
        req.user_id,
        req.request_id,
        req.conversation.id,
        json.dumps(sanitize_payload(req.model_dump())),
    )

    async def event_stream():
        ws_tok = agent_workspace.set(workspace_path)
        repo_tok = agent_repo_url.set(req.repo.url)
        gh_tok = agent_token.set(req.repo.token)
        model_tok = agent_model.set(req.model)
        wl_tok = conv_whitelist.set(req.conversation.account_whitelist)
        api_tok = agent_api_key.set(req.api_key)
        uid_tok = agent_user_id.set(req.user_id)
        rid_tok = agent_request_id.set(req.request_id)

        try:
            async for chunk in _agent.stream(
                query=req.query,
                prior=req.messages,
                conversation_meta={
                    "id": req.conversation.id,
                    "name": "agent-chat",
                    "tag": req.conversation.tag,
                    "workspace": workspace_path,
                },
                api_key=req.api_key,
                model=req.model,
            ):
                if chunk.get("type") == "history_snapshot":
                    yield f"data: {json.dumps(chunk, default=str)}\n\n"
                else:
                    yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.exception("agent-chat error")
            fatal = {"type": "fatal", "code": "INTERNAL_ERROR", "message": str(e)}
            yield f"data: {json.dumps(fatal)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            conv_whitelist.reset(wl_tok)
            agent_request_id.reset(rid_tok)
            agent_user_id.reset(uid_tok)
            agent_api_key.reset(api_tok)
            agent_model.reset(model_tok)
            agent_token.reset(gh_tok)
            agent_repo_url.reset(repo_tok)
            agent_workspace.reset(ws_tok)
            shutil.rmtree(workspace_path, ignore_errors=True)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


# ---------------------------------------------------------------------------
# POST /agent/stats — spending stats scoped by conversation tag, JSON
# ---------------------------------------------------------------------------

@app.post("/agent/stats")
async def agent_stats(req: StatsRequest):
    workspace_path = f"/tmp/bean_workspace_{uuid.uuid4().hex[:12]}"
    start_time = time.monotonic()

    logger.info(
        "agent-stats user_id=%s request_id=%s tag=%s sanitized_body=%s",
        req.user_id,
        req.request_id,
        req.conversation.tag,
        json.dumps(sanitize_payload(req.model_dump())),
    )

    tag = req.conversation.tag
    if not tag:
        return _error_envelope("INVALID_REQUEST", "conversation.tag is required", 400)

    try:
        ws.ensure_workspace(workspace_path, req.repo.url, req.repo.token)
    except Exception as e:
        return _repo_error_to_envelope(e)

    # Query spending by account for the given tag
    tag_clean = tag.lstrip("#")
    bql = (
        f'SELECT account, sum(position) AS total '
        f'WHERE tags("{tag_clean}") GROUP BY account ORDER BY total DESC'
    )
    rows, error = bc.run_bql_rows(workspace_path, bql)

    if error is not None:
        # Fallback: search narration for the tag text
        bql = (
            f'SELECT account, sum(position) AS total '
            f'WHERE narration ~ "{tag}" GROUP BY account ORDER BY total DESC'
        )
        rows, error = bc.run_bql_rows(workspace_path, bql)

    shutil.rmtree(workspace_path, ignore_errors=True)

    if error is not None:
        return JSONResponse(
            content={
                "status": "error",
                "error": {"code": "INTERNAL_ERROR", "message": error},
                "tag": tag,
                "usage": {"duration_ms": int((time.monotonic() - start_time) * 1000)},
            },
            status_code=500,
        )

    return {
        "status": "ok",
        "tag": tag,
        "rows": rows,
        "usage": {"duration_ms": int((time.monotonic() - start_time) * 1000)},
    }


# ---------------------------------------------------------------------------
# POST /agent/accounts — valid account prefixes, JSON
# ---------------------------------------------------------------------------

@app.post("/agent/accounts")
async def agent_accounts(req: AccountsRequest):
    workspace_path = f"/tmp/bean_workspace_{uuid.uuid4().hex[:12]}"
    start_time = time.monotonic()

    logger.info(
        "agent-accounts user_id=%s request_id=%s sanitized_body=%s",
        req.user_id,
        req.request_id,
        json.dumps(sanitize_payload(req.model_dump())),
    )

    try:
        ws.ensure_workspace(workspace_path, req.repo.url, req.repo.token)
    except Exception as e:
        return _repo_error_to_envelope(e)

    # Check sidecar include
    if not state._check_sidecar_include(workspace_path):
        shutil.rmtree(workspace_path, ignore_errors=True)
        return _error_envelope(
            "SETUP_REQUIRED",
            "Sidecar include directive is missing from data/main.beancount. "
            'Add: include "agent_inc/main.beancount"',
            400,
        )

    try:
        accounts = state.get_accounts(workspace_path)
        raw_accounts = _get_raw_open_directives(workspace_path)
    finally:
        shutil.rmtree(workspace_path, ignore_errors=True)

    return {
        "status": "ok",
        "accounts": accounts,
        "raw_accounts": raw_accounts,
        "usage": {"duration_ms": int((time.monotonic() - start_time) * 1000)},
    }


# ---------------------------------------------------------------------------
# POST /agent/run — DEPRECATED, forwards to /agent/chat
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
    model: str = os.environ.get("OPENAI_MODEL", "gpt-4o")


@app.post("/agent/run")
async def agent_run(req: AgentRunRequest):
    """DEPRECATED — use POST /agent/chat instead. Forwards internally."""
    logger.warning(
        "Deprecated /agent/run called; forward to /agent/chat. "
        "repo_url=%s model=%s",
        req.repo_url,
        req.model,
    )
    chat_req = ChatRequest(
        repo=RepoInfo(url=req.repo_url, token=req.token),
        user_id="deprecated-agent-run",
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        model=req.model,
        query=req.query,
        conversation=ChatConversationMeta(
            tag=req.conversation.tag,
            account_whitelist=req.conversation.account_whitelist,
        ),
        messages=req.messages,
    )
    return await agent_chat(chat_req)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

@click.command()
@click.option("--host", default=os.environ.get("AGENT_HOST", "0.0.0.0"))
@click.option("--port", default=int(os.environ.get("AGENT_PORT", "8000")), type=int)
def main(host: str, port: int):
    logger.info(f"Starting agent-core at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
