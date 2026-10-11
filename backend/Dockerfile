# Single-stage API image. Task #13 replaces this with the production image
# (frontend build, healthcheck wiring).
FROM python:3.12-slim

# uv is the only Python toolchain here: no pip, no globally installed packages.
COPY --from=ghcr.io/astral-sh/uv:0.9.13 /uv /bin/uv

# The virtualenv lives outside the working directory so that /app holds source
# only, and a bytecode cache is never written.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
