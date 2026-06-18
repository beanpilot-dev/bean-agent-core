FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN uv sync --locked --no-dev

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONPATH=/app/src

# LangFuse tracing (optional)
# ENV LANGFUSE_ENABLED=true
# ENV LANGFUSE_HOST=http://localhost:3000
# ENV LANGFUSE_PUBLIC_KEY=pk-...
# ENV LANGFUSE_SECRET_KEY=sk-...
# ENV LANGFUSE_TRACE_LEVEL=full

EXPOSE 8000

CMD ["/app/.venv/bin/python", "-m", "agent_core.main", "--host", "0.0.0.0", "--port", "8000"]
