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

from agent_core.agent import (
    PersonalFinanceAgent,
    generate_conversation_title,
    validate_model_name,
)
from agent_core.config import WORKSPACE_TTL_SECONDS
from agent_core.context import (
    agent_api_key,
    agent_model,
    agent_repo_url,
    agent_request_id,
    agent_user_id,
)
from agent_core.services.orchestrator import AgentOrchestrator
from agent_core.services.types import LedgerConfig
from agent_core.services.workspace import CachedWorkspaceManager, GitService

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

_git_service = GitService.from_environment(
    os.environ.get("AGENT_MODE", ""),
    os.environ.get("LOCAL_REPO_URL", ""),
)
_agent = PersonalFinanceAgent()
_cache_manager = CachedWorkspaceManager(_git_service, ttl_seconds=WORKSPACE_TTL_SECONDS)
_orchestrator = AgentOrchestrator(_agent, _cache_manager, _git_service)

try:
    _cache_manager.cleanup_expired()
except Exception:
    logger.warning("Cache cleanup at startup failed", exc_info=True)


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


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class RepoInfo(BaseModel):
    url: str
    token: str


class ChatConversationMeta(BaseModel):
    id: str | None = None
    tag: str | None = None
    account_whitelist: list[str] | None = None


class StatsConversationMeta(BaseModel):
    tag: str


class LedgerPayload(BaseModel):
    entry_path: str
    sidecar_main_path: str
    sidecar_write_dir: str


class ChatRequest(BaseModel):
    repo: RepoInfo
    user_id: str
    request_id: str | None = None
    agent_run_id: str | None = None
    api_key: str
    model: str = os.environ.get("OPENAI_MODEL", "gpt-4o")
    query: str
    conversation: ChatConversationMeta = ChatConversationMeta()
    messages: list[dict] = []
    ledger: LedgerPayload | None = None


class StatsRequest(BaseModel):
    repo: RepoInfo
    user_id: str
    request_id: str | None = None
    conversation: StatsConversationMeta
    ledger: LedgerPayload | None = None


class AccountsRequest(BaseModel):
    repo: RepoInfo
    user_id: str
    request_id: str | None = None
    ledger: LedgerPayload | None = None


class ConversationTitleRequest(BaseModel):
    user_id: str
    request_id: str | None = None
    api_key: str
    model: str = os.environ.get("OPENAI_MODEL", "gpt-4o")
    query: str


class OnboardingDiscoveryRequest(BaseModel):
    repo: RepoInfo
    user_id: str
    request_id: str | None = None
    entry_path: str | None = None
    expected_head_sha: str | None = None


class OnboardingSetupRequest(BaseModel):
    repo: RepoInfo
    user_id: str
    request_id: str | None = None
    operation: str
    entry_path: str | None = None
    sidecar_main_path: str | None = None
    sidecar_write_dir: str | None = None
    expected_head_sha: str | None = None


def _ledger_config(payload: LedgerPayload | None) -> LedgerConfig | None:
    if payload is None:
        return None
    return LedgerConfig(
        entry_path=payload.entry_path,
        sidecar_main_path=payload.sidecar_main_path,
        sidecar_write_dir=payload.sidecar_write_dir,
    )




# ---------------------------------------------------------------------------
# Exception handler — sanitize sensitive fields in error responses
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Log metadata-only details on unhandled exceptions."""
    logger.error(
        "Unhandled exception for %s %s request_id=%s error_type=%s",
        request.method,
        request.url.path,
        request.headers.get("x-request-id"),
        type(exc).__name__,
    )
    return _error_envelope("INTERNAL_ERROR", "Internal agent-core error", 500)


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
    try:
        ledger_config = _ledger_config(req.ledger)
    except ValueError:
        return _error_envelope("INVALID_LEDGER_CONFIG", "Invalid ledger config", 400)
    try:
        req.model = validate_model_name(req.model)
    except ValueError as e:
        logger.error("agent-chat invalid model configuration: %s", e)
        return _error_envelope("INVALID_MODEL_CONFIG", str(e), 400)

    logger.info(
        "agent-chat user_id=%s request_id=%s conv_id=%s message_count=%s "
        "has_tag=%s whitelist_count=%s model=%s",
        req.user_id,
        req.request_id,
        req.conversation.id,
        len(req.messages),
        bool(req.conversation.tag),
        len(req.conversation.account_whitelist or []),
        req.model,
    )

    async def event_stream():
        repo_tok = agent_repo_url.set(req.repo.url)
        model_tok = agent_model.set(req.model)
        api_tok = agent_api_key.set(req.api_key)
        uid_tok = agent_user_id.set(req.user_id)
        rid_tok = agent_request_id.set(req.request_id)

        try:
            async for chunk in _orchestrator.run(
                workspace_path=workspace_path,
                repo_url=req.repo.url,
                token=req.repo.token,
                agent_run_id=req.agent_run_id,
                user_id=req.user_id,
                request_id=req.request_id,
                api_key=req.api_key,
                model=req.model,
                query=req.query,
                conversation_meta={
                    "id": req.conversation.id,
                    "name": "agent-chat",
                    "tag": req.conversation.tag,
                    "account_whitelist": req.conversation.account_whitelist,
                },
                messages=req.messages,
                ledger_config=ledger_config,
            ):
                if chunk.get("type") == "history_snapshot":
                    yield f"data: {json.dumps(chunk, default=str)}\n\n"
                else:
                    yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            logger.error("agent-chat error error_type=%s", type(e).__name__)
            fatal = {"type": "fatal", "code": "INTERNAL_ERROR", "message": str(e)}
            yield f"data: {json.dumps(fatal)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            agent_request_id.reset(rid_tok)
            agent_user_id.reset(uid_tok)
            agent_api_key.reset(api_tok)
            agent_model.reset(model_tok)
            agent_repo_url.reset(repo_tok)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


# ---------------------------------------------------------------------------
# POST /agent/conversation-title — lightweight title generation, JSON
# ---------------------------------------------------------------------------

@app.post("/agent/conversation-title")
async def agent_conversation_title(req: ConversationTitleRequest):
    start_time = time.monotonic()
    try:
        model = validate_model_name(req.model)
    except ValueError as e:
        logger.error("agent-conversation-title invalid model configuration: %s", e)
        return _error_envelope("INVALID_MODEL_CONFIG", str(e), 400)

    logger.info(
        "agent-conversation-title user_id=%s request_id=%s model=%s query_length=%s",
        req.user_id,
        req.request_id,
        model,
        len(req.query),
    )
    title = await generate_conversation_title(req.query, req.api_key, model)
    if not title:
        return _error_envelope("TITLE_UNAVAILABLE", "Title generation failed", 502)
    return {
        "status": "ok",
        "title": title,
        "usage": {"duration_ms": int((time.monotonic() - start_time) * 1000)},
    }


# ---------------------------------------------------------------------------
# POST /agent/stats — spending stats scoped by conversation tag, JSON
# ---------------------------------------------------------------------------

@app.post("/agent/stats")
async def agent_stats(req: StatsRequest):
    start_time = time.monotonic()
    try:
        ledger_config = _ledger_config(req.ledger)
    except ValueError:
        return _error_envelope("INVALID_LEDGER_CONFIG", "Invalid ledger config", 400)

    logger.info(
        "agent-stats user_id=%s request_id=%s has_tag=%s",
        req.user_id,
        req.request_id,
        bool(req.conversation.tag),
    )

    tag = req.conversation.tag
    if not tag:
        return _error_envelope("INVALID_REQUEST", "conversation.tag is required", 400)

    result = await _orchestrator.run_stats(
        repo_url=req.repo.url,
        token=req.repo.token,
        user_id=req.user_id,
        request_id=req.request_id,
        tag=tag,
        ledger_config=ledger_config,
    )

    if result.get("status") == "error":
        error = result.get("error", {})
        return JSONResponse(
            content={
                "status": "error",
                "error": error,
                "tag": tag,
                "usage": {"duration_ms": int((time.monotonic() - start_time) * 1000)},
            },
            status_code=500,
        )

    return {
        "status": "ok",
        "tag": tag,
        "rows": result.get("rows", []),
        "usage": {"duration_ms": int((time.monotonic() - start_time) * 1000)},
    }


# ---------------------------------------------------------------------------
# POST /agent/accounts — valid account prefixes, JSON
# ---------------------------------------------------------------------------

@app.post("/agent/accounts")
async def agent_accounts(req: AccountsRequest):
    start_time = time.monotonic()
    try:
        ledger_config = _ledger_config(req.ledger)
    except ValueError:
        return _error_envelope("INVALID_LEDGER_CONFIG", "Invalid ledger config", 400)

    logger.info(
        "agent-accounts user_id=%s request_id=%s",
        req.user_id,
        req.request_id,
    )

    result = await _orchestrator.run_accounts(
        repo_url=req.repo.url,
        token=req.repo.token,
        user_id=req.user_id,
        request_id=req.request_id,
        ledger_config=ledger_config,
    )

    if result.get("status") == "error":
        error = result.get("error", {})
        return JSONResponse(
            content={"status": "error", "error": error},
            status_code=400 if error.get("code") == "SETUP_REQUIRED" else 500,
        )

    return {
        "status": "ok",
        "accounts": result.get("accounts", []),
        "raw_accounts": result.get("raw_accounts", []),
        "usage": {"duration_ms": int((time.monotonic() - start_time) * 1000)},
    }


# ---------------------------------------------------------------------------
# POST /agent/onboarding/discover — deterministic repo discovery, JSON
# ---------------------------------------------------------------------------

@app.post("/agent/onboarding/discover")
async def agent_onboarding_discover(req: OnboardingDiscoveryRequest):
    start_time = time.monotonic()
    logger.info(
        "agent-onboarding-discover user_id=%s request_id=%s has_entry_path=%s "
        "has_expected_head=%s",
        req.user_id,
        req.request_id,
        bool(req.entry_path),
        bool(req.expected_head_sha),
    )
    result = await _orchestrator.run_onboarding_discovery(
        repo_url=req.repo.url,
        token=req.repo.token,
        user_id=req.user_id,
        request_id=req.request_id,
        entry_path=req.entry_path,
        expected_head_sha=req.expected_head_sha,
    )
    status_code = 200 if result.get("status") != "error" else 400
    result["request_id"] = req.request_id
    result["usage"] = {"duration_ms": int((time.monotonic() - start_time) * 1000)}
    return JSONResponse(content=result, status_code=status_code)


def _valid_setup_operation(operation: str) -> bool:
    return operation in {"initialize_ledger", "install_sidecar"}


@app.post("/agent/onboarding/setup/preview")
async def agent_onboarding_setup_preview(req: OnboardingSetupRequest):
    start_time = time.monotonic()
    if not _valid_setup_operation(req.operation):
        return _error_envelope("INVALID_REQUEST", "Unsupported setup operation", 400)
    logger.info(
        "agent-onboarding-setup-preview user_id=%s request_id=%s operation=%s",
        req.user_id,
        req.request_id,
        req.operation,
    )
    result = await _orchestrator.run_onboarding_setup_preview(
        repo_url=req.repo.url,
        token=req.repo.token,
        user_id=req.user_id,
        request_id=req.request_id,
        operation=req.operation,  # type: ignore[arg-type]
        entry_path=req.entry_path,
        sidecar_main_path=req.sidecar_main_path,
        sidecar_write_dir=req.sidecar_write_dir,
    )
    result["request_id"] = req.request_id
    result["usage"] = {"duration_ms": int((time.monotonic() - start_time) * 1000)}
    return JSONResponse(
        content=result,
        status_code=200 if result.get("status") == "preview" else 400,
    )


@app.post("/agent/onboarding/setup/confirm")
async def agent_onboarding_setup_confirm(req: OnboardingSetupRequest):
    start_time = time.monotonic()
    if not _valid_setup_operation(req.operation):
        return _error_envelope("INVALID_REQUEST", "Unsupported setup operation", 400)
    if req.expected_head_sha is None:
        return _error_envelope("INVALID_REQUEST", "expected_head_sha is required", 400)
    logger.info(
        "agent-onboarding-setup-confirm user_id=%s request_id=%s operation=%s "
        "has_expected_head=%s",
        req.user_id,
        req.request_id,
        req.operation,
        bool(req.expected_head_sha),
    )
    result = await _orchestrator.run_onboarding_setup_confirm(
        repo_url=req.repo.url,
        token=req.repo.token,
        user_id=req.user_id,
        request_id=req.request_id,
        operation=req.operation,  # type: ignore[arg-type]
        expected_head_sha=req.expected_head_sha,
        entry_path=req.entry_path,
        sidecar_main_path=req.sidecar_main_path,
        sidecar_write_dir=req.sidecar_write_dir,
    )
    result["request_id"] = req.request_id
    result["usage"] = {"duration_ms": int((time.monotonic() - start_time) * 1000)}
    success_statuses = {"success", "stale", "validation_failed"}
    return JSONResponse(
        content=result,
        status_code=200 if result.get("status") in success_statuses else 400,
    )


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
        "model=%s",
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
