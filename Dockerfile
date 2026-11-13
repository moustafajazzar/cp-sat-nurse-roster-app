# One image per release. The containers choose their command at start: see
# deploy/compose.yaml.

# ---- the SPA --------------------------------------------------------------------------------
FROM node:26-slim AS spa
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- the Python environment: the backend and the solver -------------------------------------
FROM python:3.14-slim-trixie AS python
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# The dependencies in a layer of their own: most changes do not touch them.
COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml backend/
COPY solver/pyproject.toml solver/README.md solver/
RUN uv sync --frozen --all-packages --no-install-workspace
COPY backend/ backend/
COPY solver/nurse_roster/ solver/nurse_roster/
COPY solver/solver_service/ solver/solver_service/
RUN uv sync --frozen --all-packages --no-editable

# ---- the image ------------------------------------------------------------------------------
FROM python:3.14-slim-trixie
# The worker's nightly backup runs `pg_dump`, which refuses a server newer than itself, and Debian's own client is 17: the
# PostgreSQL project's repository has the server's version.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates postgresql-common \
 && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y \
 && apt-get install -y --no-install-recommends postgresql-client-18 \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 app && install -d -o app -g app -m 0700 /backups
COPY --from=python --chown=app:app /app /app
COPY --from=spa --chown=app:app /src/dist /app/spa
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 SPA_DIR=/app/spa
USER app
WORKDIR /app/backend
