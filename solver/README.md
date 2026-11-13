# nurse-roster (solver)

A monthly nurse-roster solver built on Google OR-Tools **CP-SAT**, with an HTTP service around it.

You give it one **request** describing a single unit for a single month: the rules, the weights and the nurses, with
every value spelled out and every day written as a date. It returns one **result**: the roster as nurse × date, any rule
the roster breaks, a cost broken down by rule, and per-nurse hours accounting. The result also holds a `carry_over` block
that feeds directly into next month's request.

The solver has no database and no built-in defaults. It holds no settings for any particular unit, so everything it
uses comes from the request.

## Contents

| Path | What it is |
|------|------------|
| `nurse_roster/` | The solver library and its command-line interface |
| `solver_service/` | A FastAPI service wrapping the library: solve jobs, validation, precheck |
| `tests/`, `service_tests/` | Library tests and HTTP contract tests |
| `tests/fixtures/er_request_2026-03.json` | A full sample: an anonymised 25-nurse emergency unit, March 2026 |
| `service_tests/fixtures/four_nurses*.json` | A minimal four-nurse request, with and without skills |
| [`how-to.md`](how-to.md) | Usage guide: writing requests, reading results, month-to-month use, re-plans |
| [`DESIGN.md`](DESIGN.md) | How it works: the model, every rule and its encoding, the data contract |

## Quick start

Requires Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python -m nurse_roster solve tests/fixtures/er_request_2026-03.json --out result.json --csv roster.csv
uv run python -m pytest -q
```

Exit codes: `0` a roster that meets every hard rule, `1` no roster found, `2` the roster breaks a hard rule, `3` the
model and the validator disagree, `4` malformed input.

## The HTTP service

```bash
SOLVER_TOKEN=change-me uv run uvicorn --factory solver_service.app:create_app --host 0.0.0.0 --port 8080
```

Every call must send `Authorization: Bearer $SOLVER_TOKEN`. Bodies and answers are JSON.

| Call | Body | Answer |
|------|------|--------|
| `POST /solve` | a request | `202 {"job_id"}`; `409 {"detail", "job_id"}` while another job runs |
| `GET /jobs/{id}` | | `state` (`running` / `finished` / `cancelled` / `failed`), `elapsed_seconds`, `best_cost`, `improvements`, `best_roster`, `result`, `error` |
| `POST /jobs/{id}/cancel` | | the job; the best roster found so far is kept |
| `POST /validate` | `{"solve_input", "roster"}` | hard violations, cost, penalties and a per-rule explanation for a given roster |
| `POST /precheck` | a request | `{"issues": [...]}` |
| `GET /health` | | `{"status", "version", "workers"}` |

A malformed request gets a `422` whose `detail` names the field at fault. The service runs one solve at a time and keeps
jobs in memory, so run it as a single process. A job ID is unknown after a restart.

## Settings

| Variable | Default | Meaning |
|----------|---------|---------|
| `SOLVER_TOKEN` | required | The bearer token for the HTTP service |
| `NURSE_ROSTER_WORKERS` | the CLI uses 8; the service uses the container's CPU count | Search threads, 1–128 |

The worker count is a server setting and is never part of a request. A request marked `reproducible` always runs on one
worker.
