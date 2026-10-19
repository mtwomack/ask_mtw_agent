# Cloud Run image for the chat (see deploy/deploy.py)
FROM python:3.12-slim

# uv for fast, locked installs
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# Dependencies first (cached layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# App code only - no .env, no local data (see .gcloudignore)
COPY main.py services.py auth.py chat.html ./
COPY agents_mtw_agent ./agents_mtw_agent

# Non-root user
RUN useradd -m appuser && chown -R appuser /app
USER appuser

EXPOSE 8080
CMD ["/app/.venv/bin/python", "main.py"]
