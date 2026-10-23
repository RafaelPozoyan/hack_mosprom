# Панель (React) собирается и раздаётся тем же сервисом FastAPI — один контейнер приложения.
FROM oven/bun:1.3 AS web
WORKDIR /web
COPY web/package.json web/bun.lock ./
RUN bun install --frozen-lockfile
COPY web/ ./
RUN bun run build

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /bin/uv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev
COPY backend/ ./
COPY --from=web /web/dist /web/dist
ENV PATH="/app/.venv/bin:$PATH"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
