# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A hospital nurse-rostering app: a Django + django-ninja JSON API and queue worker, a React SPA, and a CP-SAT (OR-Tools)
solver behind its own HTTP service. All three ship in **one Docker image**; `deploy/compose.yaml` picks each container's
command at start. The root `README.md` covers features, deployment and development; `solver/how-to.md` and
`solver/DESIGN.md` cover the solver's request/result contract and model.

Comments and docstrings explain why the code does what it does. Keep them free of development-process references (ticket
numbers, ADR numbers, review history, links to documents that are not in the repo).

## Layout

- `pyproject.toml` + `uv.lock`: a **uv workspace** with members `backend` and `solver`. One lock and one `.venv` for both.
  Python is pinned to 3.14 (`>=3.14,<3.15`, because OR-Tools 9.15 has no 3.15 wheels).
- `solver/`: package `nurse-roster`, with two Python packages:
  - `nurse_roster/`: the pure solver library and CLI. It has no database and no defaults.
  - `solver_service/`: a FastAPI wrapper (`app.py`, `jobs.py`, `grid.py`) that adds no rostering logic.
  - Docs: `README.md`, `how-to.md` (request/result usage) and `DESIGN.md` (rules, encodings, data contract, and the
    dated reasoning behind each decision). Read `DESIGN.md` before changing a rule or the request/result schema.
- `backend/`: Django project `config/` plus one app per domain: accounts, audit, hospital, units, nurses, rosters, solves,
  requesting, swaps, covers, closes, notifications, ops.
- `frontend/`: Vite + React 19 + TanStack Router/Query + Tailwind 4 + shadcn (`@base-ui/react`), with zod and react-hook-form.
- `e2e/`: Playwright browser checks against throwaway compose stacks.
- `deploy/`: the production compose file, Caddyfile, `.env.example`, ops scripts, and `deploy/tests/operations.sh`.

## Commands

### Solver (run from `solver/`)
```bash
uv sync
uv run python -m pytest -q                                   # tests/ (library) + service_tests/ (HTTP contract)
uv run python -m pytest -q tests/test_rules.py::test_name    # a single test
uv run python -m nurse_roster solve tests/fixtures/er_request_2026-03.json --out result.json --csv roster.csv
```
The CLI's exit codes are 0 roster, 1 none found, 2 hard violations, 3 model/validator mismatch, 4 bad input. The worker
count comes from `--workers` or `NURSE_ROSTER_WORKERS` (default 8) and is never part of a request.

### Backend (run from `backend/`)
Tests need a real **PostgreSQL 18**. `config/test_settings.py` defaults to `localhost:5432`, user/db `nurse_roster` and
password `dev-only-postgres-password`; override with `POSTGRES_HOST`, `POSTGRES_PORT` and `POSTGRES_PASSWORD`.
Start a throwaway database with:
```bash
docker run -d --rm --name nr-pg -p 127.0.0.1:5432:5432 -e POSTGRES_USER=nurse_roster -e POSTGRES_DB=nurse_roster -e POSTGRES_PASSWORD=dev-only-postgres-password postgres:18
```
```bash
uv run pytest -q
uv run pytest -q swaps/tests/test_x.py::test_name
```
Any `manage.py` command outside the tests needs `SECRET_KEY`, `POSTGRES_PASSWORD` and `SOLVER_TOKEN` set (dummy values work
for commands that don't touch the database):
```bash
SECRET_KEY=x SOLVER_TOKEN=x POSTGRES_PASSWORD=x uv run python manage.py makemigrations --check --dry-run
```

### API schema → frontend client (keep in sync)
`frontend/openapi.json` and `frontend/src/api/generated/` are committed, and CI fails when either is stale. After any API
change:
```bash
cd backend && SECRET_KEY=x SOLVER_TOKEN=x POSTGRES_PASSWORD=x uv run python manage.py export_openapi_schema --api config.api.api --indent 2 --output ../frontend/openapi.json
```
```bash
cd frontend && npm run generate:api
```
Never hand-edit `src/api/generated/` or `src/routeTree.gen.ts` (the TanStack Router plugin generates the latter).

### Frontend (run from `frontend/`, Node ≥ 26)
```bash
npm run dev          # Vite; proxies /api to http://localhost:8000 (override with VITE_API_PROXY)
npm test             # vitest run (jsdom)
npx vitest run src/lib/zod.test.ts
npm run typecheck
npm run build
```
Running Django under Vite needs `DJANGO_DEBUG=1`, which trusts the `http://localhost:5173` origin for CSRF and turns off
secure cookies.

### Browser checks (e2e)
```bash
e2e/stack.sh up <project>            # build the image, start a seeded stack (a couple of minutes, real solves)
e2e/stack.sh up <project> --empty    # first-start stack for the journey tests
cd e2e && E2E_STACK=<seeded> E2E_JOURNEY_STACK=<empty> npx playwright test
e2e/stack.sh down <project>
e2e/ci.sh                            # the whole CI run: both stacks, every Playwright project, deploy/tests/operations.sh
```
The project name `nurse-roster` is refused because it belongs to the real installation. Specs must import `test` from
`e2e/support.ts`, not from `@playwright/test` (the support version watches for CSP violations, and the config throws
otherwise). The checks share a stack and run serially (`workers: 1`).

### Before calling a change done
- The exported OpenAPI schema matches `frontend/openapi.json`, and the generated client is regenerated from it.
- `manage.py makemigrations --check --dry-run` prints "No changes detected".
- The backend, solver and frontend suites pass, and `npm run build` succeeds. A frontend test that waits on a clock or on
  render order can pass alone and fail under load, so run `npm test` a few times in parallel when touching timing.

## Architecture

### Runtime topology (`deploy/compose.yaml`)
`caddy` (TLS) → `web` (gunicorn gthread, 3×4 threads; runs `manage.py prepare` first to migrate and seed) → `db`
(Postgres 18). `worker` runs Procrastinate (`manage.py queue worker --concurrency 4`) and owns the nightly backups and the
push notifications. `solver` is uvicorn running `solver_service.app:create_app`. It holds only `SOLVER_TOKEN`, no database
credentials, and sits on an internal network. Only the worker has internet egress, and only to push services. **Run the
solver as one process**: its jobs live in memory.

### A solve, end to end
1. `solves/input.py` builds a **request** from the database: one unit, one month, every rule and weight explicit, dates
   instead of indices. It draws on `hospital.rules.general_block`, `nurses.rostering.nurse_block` and others.
2. `solves/tasks.run_solve` runs on the Procrastinate queue under a **hospital-wide one-at-a-time lock**
   (`ops/queue.py`). `solves/running.py` posts to the solver, polls `/jobs/{id}`, and retries.
3. `ops/solver_client.py` is the **only** way the backend reaches the solver (Bearer token; typed exceptions
   `SolverUnreachable`, `SolverRefused` (422), `SolverBusy` (409) and `SolverJobUnknown` (the solver restarted)).
4. The solver returns a **result**: roster as nurse × date, violations, penalties, and a per-nurse `carry_over` block keyed by
   the request's own field names, so next month's request is this month's nurse records plus that block. The backend
   stores the result as a draft roster (`rosters/`). Re-plans (`published` + `replan_from`) and month close (`closes/`, a
   replan from the day after the month) reuse the same contract.
5. `recover_lost_jobs` runs every five minutes and requeues solves that a dead worker left holding the lock.

### Solver internals (`solver/nurse_roster/`)
`request.py` (load and validate; strict: every field required, unknown fields rejected, errors prefixed
`request: <path>:`) → `problem.py` (internal problem) → `model.py` (CP-SAT model and solve) → `validate.py` /
`explain.py` (an independent validator that rescores the roster) → `result.py`. `precheck.py` reports input issues before
the solve. `calendar.py` derives the day count and the Umm al-Qura Ramadan days from `month`. The model horizon is 10
fixed history days plus the month, so pattern rules cross the month boundary with no special cases.

Schema evolution has its own rules: request `version` 2 is current and version 1 is **still read** (stored inputs are the
app's history). New optional fields go in through `request._optional_rules`, which fills defaults on copies, rather than
through a new version. The result `version` stays 1. Every change gets a dated entry in `DESIGN.md`.

### Backend conventions
- `config/api.py` mounts every app's router on one `NinjaAPI(auth=SignedIn())`. Endpoints are signed-in by default and
  declare their roles with `accounts.authz.SignedIn`. `accounts/tests/test_authorization.py` walks every endpoint to
  enforce this, so a new endpoint must name its roles, or set `auth=None` explicitly.
- The OpenAPI doc is not served. It is exported only for the frontend client.
- State changes are audited through `audit.log.record`. Periodic and background work goes in each app's `tasks.py` on
  Procrastinate.
- Logs are one JSON object per line (`solver_service.logs.JsonFormatter`, also used by Django). Worker logs can name
  nurses and dates, so treat them as personal data.
- `E2E_CLOCK_FILE` (set only by `e2e/compose.e2e.yaml`) shifts the hospital's date; code reads "today" through
  `nurses.membership.today`, not `date.today()`.
- In production `web` serves the built SPA through WhiteNoise (`SPA_DIR`). Only hashed `/assets/` are cached long-term;
  `config.urls` returns `index.html` for client routes.
- Test fixtures in `backend/conftest.py`: `make_user`, `signed_in(role)`, `client_of(username, role)` and `post(...)`.
  Autouse fixtures block real backups and real push services, and reset the cached health answer.

### Frontend conventions
- File-based routes in `src/routes/`. Authenticated pages live under `_signed-in/`, and a `name_.print.tsx` route is a print
  layout outside its parent.
- All API calls go through the generated react-query hooks, which use `src/api/fetcher.ts` (`apiFetch`: adds the CSRF
  header and throws `ApiError` on any non-2xx).
- `import … from "zod"` is aliased to `src/lib/zod.ts` (zod with `jitless`, so the content security policy needs no `unsafe-eval`).
- User-facing wording lives in `src/lib/*-words.ts` modules.
- The app is a PWA (`public/sw.js`, `src/lib/stale-build.ts`) and must hold its layout down to 320 px wide.
