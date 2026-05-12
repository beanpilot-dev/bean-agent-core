"""Agent Core — stateless Beancount ledger agent (GPL 2.0).

Accepts per-request repo credentials and executes ledger operations in an
ephemeral environment. No persistent state — the caller owns conversation
history and persistence.

API:
    POST /agent/run  →  SSE stream of agent responses
    GET  /health     →  liveness check
"""

__version__ = "0.1.0"
