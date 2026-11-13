# Nurse Roster

A self-hosted web app that builds monthly hospital nurse rosters with Google OR-Tools **CP-SAT**. Each roster follows
hospital-wide and per-unit rules on hours, rest, coverage, skills and fairness. The app then runs the month after the solve:
staff requests, re-plans, shift swaps, sick-leave covers and month-end close.

The app sends the solver one explicit **request** per unit and month. The solver returns the roster along with every rule
it broke, what each one cost, and a per-nurse carry-over block that feeds next month's request.

---

## Features

**Rostering**
- One CP-SAT model per unit and month. Hard rules include one shift per day, forbidden shift successions, headcount ranges
  per shift (weekday and Friday), a maximum run of consecutive shifts and rest after it, rest after nights, at least one
  team leader and both genders on every shift, allowed shifts per nurse, leave and pinned cells.
- Soft rules carry configurable weights: overtime and shortfall against contract hours, day/night and Friday fairness,
  undesired and desired days, skill coverage, full runs, a shared overtime target, and preceptor/probation pairing.
- Fairness carries across months: night, Friday, hours and overtime balances move into the next month.
- The calendar comes from the month: Ramadan days (Umm al-Qura, with manual override), public holidays, and reduced
  hours during Ramadan and for breastfeeding.
- Re-plans keep the published roster up to a chosen date and re-solve the rest. Month close reconciles what was actually
  worked.
- Reproducible mode uses one worker, a fixed seed and deterministic time, so the same request gives the same roster on
  any machine.
- An independent validator re-scores every roster, and a precheck reports input problems before the solve.

**The app**
- Five roles: System Admin, CNO, Deputy CNO, Unit Manager and Staff Nurse, each with its own pages.
- Hospital catalogue: shifts, leave types, skills (with expiries), holidays and Ramadan, and hospital rules.
- Units, nurses (CSV import of nurses and skills), unit rules and rostering attributes.
- Request windows for staff preferences and leave, draft review, roster versions and notes, solve history with result
  comparison.
- Shift swaps and sick-leave covers between nurses, checked against the rules before they take effect.
- Web push notifications, an audit log, Excel export and print layouts.
- An installable PWA that works down to 320 px wide.
- Operations: health page, Prometheus `/metrics`, nightly `pg_dump` backups, scripted restore and admin password
  reset, JSON logs.

---

## Architecture

```
            browser (React SPA / PWA)
                     │ HTTPS
                ┌────▼────┐
                │  Caddy  │  TLS (Let's Encrypt or self-signed)
                └────┬────┘
                ┌────▼────┐          ┌──────────┐
                │   web   │──────────▶   db     │ PostgreSQL 18
                │ Django  │          └────▲─────┘
                └────┬────┘               │
                     │ Procrastinate queue│
                ┌────▼────┐               │
                │ worker  │───────────────┘──▶ browser push services
                └────┬────┘
                     │ HTTP + bearer token
                ┌────▼────┐
                │ solver  │  FastAPI + OR-Tools CP-SAT
                └─────────┘
```

One Docker image holds all three components, and each container picks its command at start (`deploy/compose.yaml`):

| Service  | What it runs | Notes |
|----------|--------------|-------|
| `caddy`  | Caddy 2 | The only public entry point. |
| `web`    | gunicorn (gthread, 3×4) serving the Django + django-ninja JSON API and the built SPA (WhiteNoise) | Runs migrations and seeds data on start. |
| `worker` | Procrastinate queue worker (concurrency 4) | Solves, scheduled jobs, backups, push. Only one solve runs at a time across the hospital. |
| `solver` | uvicorn `solver_service.app:create_app` | Holds no database credentials. Jobs live in memory, so run a single process. |
| `db`     | PostgreSQL 18 | |

Every internal network except the worker's push egress is `internal: true`.

**A solve, end to end:** the backend builds a request from the database (`backend/solves/input.py`). It queues the solve
(`solves/tasks.py`), which posts the request to the solver (`ops/solver_client.py`) and polls until the job ends. The result
is stored as a draft roster for the Unit Manager to review and publish.

### Tech stack

| Layer | Stack |
|-------|-------|
| Solver | Python 3.14, OR-Tools 9.15 (CP-SAT), FastAPI, hijridate |
| Backend | Django 6.1, django-ninja, Procrastinate, psycopg 3, pywebpush, xlsxwriter |
| Frontend | React 19, TypeScript 7, Vite 8, TanStack Router / Query / Table, Tailwind CSS 4, shadcn/ui (Base UI), zod, react-hook-form, Orval-generated API client |
| Tests | pytest, pytest-django, Vitest + Testing Library, Playwright |
| Deploy | Docker Compose, Caddy, PostgreSQL 18 |

---

## Repository layout

```
backend/     Django project (config/) and one app per domain: accounts, audit, hospital, units, nurses,
             rosters, solves, requesting, swaps, covers, closes, notifications, ops
solver/      nurse_roster/ (the solver library + CLI) and solver_service/ (its HTTP service)
             README.md, how-to.md (request/result guide), DESIGN.md (rules, encodings, data contract)
frontend/    the SPA; openapi.json and src/api/generated/ are generated from the backend
e2e/         Playwright browser checks and scripts that start throwaway stacks
deploy/      compose.yaml, Caddyfile, .env.example, restore / admin-reset scripts, operations tests
```

The backend and solver form one [uv](https://docs.astral.sh/uv/) workspace, with a single `uv.lock` and one virtual
environment.

---

## Deploying

Requirements: Docker with Compose v2.24 or later.

```bash
cd deploy
cp .env.example .env
# Fill SECRET_KEY, POSTGRES_PASSWORD, SOLVER_TOKEN and INITIAL_ADMIN_PASSWORD, e.g. with:
#   openssl rand -base64 48 | tr -d '/+=\n'
docker compose up -d --build
```

- Sign in as `admin` with `INITIAL_ADMIN_PASSWORD`. The app forces a password change at first login.
- Set `DOMAIN` (with DNS pointing at the server and ports 80/443 open) to get a Let's Encrypt certificate. If it is empty,
  Caddy serves a self-signed certificate.
- `TIME_ZONE` defaults to `Asia/Riyadh`. `TRUSTED_PROXIES` lists the hospital's own reverse proxies, if any.
- Backups are written nightly to the `backups` volume. Restore one with `deploy/restore.sh <backup>`.
- If the System Admin password is lost, run `deploy/reset-admin-password.sh`.

### Push notifications

Only the `worker` container reaches the internet, and only to send browser push notifications. If the server sits behind
a firewall, allow outbound HTTPS (port 443) to these hosts:

- `fcm.googleapis.com`: Chrome, Edge and other Chromium browsers on Android and desktop
- `push.services.mozilla.com`, `*.push.services.mozilla.com`: Firefox
- `*.push.apple.com`: Safari on iOS and macOS
- `*.notify.windows.com`: Windows

The Health page reports when sending fails because the push service could not be reached.

---

## Development

Requirements: Python 3.14 and `uv`, Node 26 or later, Docker (for PostgreSQL and the browser checks).

### Solver

```bash
cd solver
uv sync
uv run python -m nurse_roster solve tests/fixtures/er_request_2026-03.json --out result.json --csv roster.csv
uv run python -m pytest -q
```

The sample is an anonymised 25-nurse emergency unit (nurses `N01`–`N25`) for March 2026. CLI exit codes: `0` roster found, `1` none found, `2`
hard violations, `3` model/validator mismatch, `4` malformed input. Set the worker count with `--workers N` or
`NURSE_ROSTER_WORKERS` (default 8). See [`solver/how-to.md`](solver/how-to.md) to write your own requests.

### Backend

The tests run against a real PostgreSQL:

```bash
docker run -d --rm --name nr-pg -p 127.0.0.1:5432:5432 \
  -e POSTGRES_USER=nurse_roster -e POSTGRES_DB=nurse_roster \
  -e POSTGRES_PASSWORD=dev-only-postgres-password postgres:18
cd backend
uv run pytest -q
```

Override `POSTGRES_HOST`, `POSTGRES_PORT` or `POSTGRES_PASSWORD` to use another database. Any other `manage.py` command
needs `SECRET_KEY`, `POSTGRES_PASSWORD` and `SOLVER_TOKEN` in the environment. Set `DJANGO_DEBUG=1` to run Django behind the
Vite dev server.

### Frontend

```bash
cd frontend
npm ci
npm run dev        # proxies /api to http://localhost:8000 (VITE_API_PROXY overrides)
npm test
npm run typecheck
npm run build
```

### Keeping the API client in sync

The OpenAPI document and the generated client are committed. Regenerate both after any API change:

```bash
cd backend && SECRET_KEY=x SOLVER_TOKEN=x POSTGRES_PASSWORD=x \
  uv run python manage.py export_openapi_schema --api config.api.api --indent 2 --output ../frontend/openapi.json
cd ../frontend && npm run generate:api
```

### Browser checks

```bash
e2e/stack.sh up my-stack            # build and start a seeded stack (real solves; a couple of minutes)
e2e/stack.sh up my-journey --empty  # a first-start stack for the journey tests
cd e2e && npm ci && npx playwright install chromium
E2E_STACK=my-stack E2E_JOURNEY_STACK=my-journey npx playwright test
e2e/stack.sh down my-stack && e2e/stack.sh down my-journey
```

`e2e/ci.sh` runs the whole browser suite plus `deploy/tests/operations.sh` from start to finish.

### Before opening a pull request

- Regenerate the OpenAPI schema and the client (above) and commit any difference.
- `SECRET_KEY=x SOLVER_TOKEN=x POSTGRES_PASSWORD=x uv run python manage.py makemigrations --check --dry-run` (from
  `backend/`) reports no changes.
- The backend, solver and frontend suites pass, and `npm run build` succeeds.

---

## Further reading

- [`solver/how-to.md`](solver/how-to.md): building requests, reading results, carry-over, re-plans, month close.
- [`solver/DESIGN.md`](solver/DESIGN.md): decision variables, every rule and its encoding, the data contract, benchmarks
  and review history.
- [`CLAUDE.md`](CLAUDE.md): a condensed guide to the codebase's architecture and conventions.
