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

# The hosted MCP endpoint (#43) mounts `mcp/`'s tools into this process, so the
# runtime image carries that source: the path dependency in `pyproject.toml`
# points at `../mcp`, which resolves to `/mcp` from here. The SDK comes from
# `uv sync`; the tools are the one implementation under `mcp/`, copied not
# rewritten.
COPY mcp/pyproject.toml /mcp/pyproject.toml
COPY mcp/jobradar_mcp /mcp/jobradar_mcp
RUN uv sync --frozen --no-dev

COPY backend/app ./app

# The migration path ships in the image, beside `app/`: `alembic.ini` names
# `migrations` as its script location and `migrations/env.py` takes the URL from
# `app.db.database_url()`, so the container can bring a database that already
# holds rows to this image's schema with no checkout and no second connection
# setting (#47).
COPY backend/alembic.ini ./alembic.ini
COPY backend/migrations ./migrations
COPY backend/docker-entrypoint.sh ./docker-entrypoint.sh

# The build enters the image here and nowhere else: not from the host, not from a
# mount. It lands in `app/static`, the directory `app.main` resolves from its own
# location, so the running app serves this build as the UI and there is no second
# service and no CORS.
COPY --from=frontend /build/dist ./app/static

# A user of its own, so the serving process is not root. Nothing in the image is
# written at run time except the uploads volume, so no `/app` ownership is needed
# here -- the source is read-only. The uid is fixed because a *fresh* named volume
# inherits the owner of the image's directory it covers, which is what lets the
# app write its upload into `/data/uploads`; an existing root-owned volume from an
# earlier image is not, see `README.md`.
RUN useradd --uid 10001 --user-group jobradar \
    && mkdir -p /data/uploads \
    && chown -R jobradar:jobradar /data/uploads

USER jobradar

EXPOSE 8000

# Every start goes through the upgrade first (#47): the script runs
# `alembic upgrade head` and then execs the command below, so uvicorn only ever
# starts on the schema this image carries, and an upgrade that fails exits the
# container non-zero instead of serving against a schema it does not match. The
# script is read by the shell, so it needs no executable bit in the image.
ENTRYPOINT ["/bin/sh", "/app/docker-entrypoint.sh"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
