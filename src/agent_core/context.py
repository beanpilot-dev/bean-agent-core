import contextvars

# Per-request agent execution context. Set by main.py from the request body
# for observability (traces, logs, usage). Tools receive workspace, token,
# and whitelist via InjectedToolArg — they do not read ContextVars.

agent_repo_url: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_repo_url", default=None
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
