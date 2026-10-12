# One image, built from the repository root: an earlier stage builds the frontend
# with the frontend's own toolchain, and the runtime stage copies that build in
# beside the API. Both stages are pinned tags, never `latest`.
#
# The context is the repository root, not `backend/`, because the frontend stage
# needs `frontend/package.json`, `frontend/package-lock.json` and `frontend/src`.
# `.dockerignore` at the root is what keeps that context to source and the two
# lock files.

FROM node:22.12-alpine AS frontend

WORKDIR /build

# The lock file and the manifest alone, so the install layer survives a source
# change. `npm ci` installs exactly what the lock file pins, dev dependencies
# included: `npm run build` is `tsc -b && vite build`.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim AS runtime

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

COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev

COPY backend/app ./app

# The build enters the image here and nowhere else: not from the host, not from a
# mount. It lands in `app/static`, the directory `app.main` resolves from its own
# location, so the running app serves this build as the UI and there is no second
# service and no CORS.
COPY --from=frontend /build/dist ./app/static

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
