import contextvars

# Per-request account whitelist. Set by the caller before invoking agent.stream();
# read by _validate_accounts in mutations.py (via agent.py tool functions).
conv_whitelist: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "conv_whitelist", default=None
)

# Per-request agent execution context. Set by main.py from the request body
# before invoking agent.stream(); read by agent tool functions.
agent_workspace: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_workspace", default=""
)

agent_repo_url: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_repo_url", default=None
)

agent_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_token", default=None
)

agent_model: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_model", default="gpt-4o"
)

# API key for LLM calls, per-request. Read from request body, never from server
# environment. Discarded after each request.
agent_api_key: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_api_key", default=None
)

# Opaque user identifier from the caller. For observability (traces, logs, usage)
# only — never used for access control or business logic.
agent_user_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_user_id", default=None
)

# Correlates agent-core call with srv request context. Echoed in response/traces.
agent_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_request_id", default=None
)
