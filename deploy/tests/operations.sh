#!/usr/bin/env bash
# The compose-level test of the installation, on a stack of its own built from this checkout: its own project, secrets, volumes, image
# tag and HTTPS port. It never touches another project, and refuses the installation's own name.
#
#   deploy/tests/operations.sh <project> [--keep]
#
# It checks, in order: the public health answer through Caddy; /metrics refused through Caddy and answered inside; one
# content security policy through Caddy, framing by no site, set deferred, and HSTS only in the DOMAIN mode's adapted
# configuration; no address a client claims in the audit log; JSON lines from web, worker and solver, and the push key pair
# made at the first start; gunicorn threaded: gthread, 3 workers of 4 threads, its default timeout, Caddy's idle time to web
# under gunicorn's keep-alive, and POSTs at gaps either side of both answered by web; Caddy holding the body: its version,
# its body timeouts, a body over 2 MB refused, and a login withholding its body not stopping a one-worker web; the worker
# alone on a network with a way out; the browser checks' clock off; a backup taken by the worker's job; the retention jobs; a
# restore by deploy/restore.sh, which takes back what was written after the backup, brings back no lockout or throttle, and
# lists the deactivation, password reset, password change and key rotation it undid: by time when the backup it restores is
# the one a restore took, saying so when there is nothing to list, and finishing when the list cannot be printed; a backup
# without the throttle's table restored; deploy/reset-admin-password.sh lifting a lockout; the System Admin's health answer
# and /metrics counting pushes sent and failed; a 1 MiB nurse import file previewed and confirmed through Caddy; a
# confirmation of 300 nurses on one CPU running past gunicorn's timeout and ending. Then it removes everything it made,
# unless --keep (the stack stays up, and the line to remove it is printed).
# TEST_HTTPS_PORT (default 18443) is where Caddy answers on 127.0.0.1. Needs docker, curl, openssl and python3.
set -euo pipefail
cd "$(dirname "$0")/.."

project=${1:?usage: $0 <project> [--keep]}
keep=${2:-}
[[ $project != nurse-roster ]] || { echo "Refused: nurse-roster is the installation's own project." >&2; exit 2; }
port=${TEST_HTTPS_PORT:-18443}
base="https://localhost:$port"

work=$(mktemp -d)
admin_password=$(openssl rand -hex 12)
cat > "$work/env" <<EOF
SECRET_KEY=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 16)
SOLVER_TOKEN=$(openssl rand -hex 16)
INITIAL_ADMIN_PASSWORD=$admin_password
APP_IMAGE=$project-app
APP_VERSION=test
TEST_HTTPS_PORT=$port
EOF
export COMPOSE_PROJECT_NAME=$project COMPOSE_FILE=compose.yaml:tests/compose.test.yaml COMPOSE_ENV_FILES=$work/env

compose() { docker compose "$@"; }
sql() { compose exec -T db psql --username nurse_roster --dbname nurse_roster --tuples-only --no-align --no-psqlrc -c "$1"; }
pass() { echo "ok - $*"; }
fail() { echo "FAILED - $*" >&2; exit 1; }
# Waits up to $1 seconds for a command to succeed: for a job to run or a service to answer, never to show a thing did not happen.
wait_for() {
  local seconds=$1; shift
  for _ in $(seq "$seconds"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}
# The handlers of one kind (`headers`, `reverse_proxy`) anywhere in an adapted Caddy configuration read on standard input,
# as a JSON list.
handlers() {
  python3 -c '
import json, sys
def walk(node):
    if isinstance(node, dict):
        if node.get("handler") == sys.argv[1]:
            yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)
print(json.dumps(list(walk(json.load(sys.stdin)))))' "$1"
}
finish() {
  status=$?
  if [[ $keep == --keep ]]; then
    echo "Kept. Remove with: COMPOSE_PROJECT_NAME=$project COMPOSE_FILE=$COMPOSE_FILE COMPOSE_ENV_FILES=$work/env docker compose down -v && docker image rm $project-app:test"
  else
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    docker image rm "$project-app:test" >/dev/null 2>&1 || true
    rm -rf "$work"
  fi
  exit $status
}
trap finish EXIT

echo "# Building and starting $project…"
compose up -d --build --wait --quiet-pull

# ---- health, metrics and logs ----------------------------------------------------------------------------------------------
wait_for 60 curl -sfk "$base/api/health" || fail "Caddy does not answer on $base"
health=$(curl -sk "$base/api/health")
python3 -c 'import json, sys; assert json.loads(sys.argv[1]) == {"status": "ok", "database": True, "solver": {"reachable": True}}, sys.argv[1]' "$health" \
  && pass "the public health answer says only reachability: $health"

code=$(curl -sk -o /dev/null -w '%{http_code}' "$base/metrics")
[[ $code == 404 ]] && pass "/metrics through Caddy: $code" || fail "/metrics through Caddy answered $code"
code=$(curl -sk -o /dev/null -w '%{http_code}' "$base/metrics/")
[[ $code == 404 ]] && pass "/metrics/ through Caddy: $code" || fail "/metrics/ through Caddy answered $code"
compose exec -T caddy wget -qO- http://web:8000/metrics | grep -qx 'nurse_roster_database_up 1' \
  && pass "/metrics on the internal network, from Caddy's container straight to web:8000" || fail "/metrics inside"

# One content security policy through Caddy on every answer, a page, a static file, an API answer and
# Caddy's own /metrics refusal, framing by no site among it; web says X-Frame-Options: DENY in its own words too. No HSTS
# without DOMAIN, as here.
policy="default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
for path in / /unit-roster /favicon.svg /api/health /metrics; do
  headers=$(curl -sk -o /dev/null -D - "$base$path" | tr -d '\r')
  policies=$(grep -ic '^content-security-policy:' <<<"$headers" || true)
  [[ $policies == 1 ]] || fail "$path through Caddy has $policies content security policies: $headers"
  grep -qixF "content-security-policy: $policy" <<<"$headers" || fail "$path through Caddy has another policy: $headers"
  [[ $path == /metrics ]] || grep -qix 'x-frame-options: DENY' <<<"$headers" || fail "$path through Caddy has no X-Frame-Options: DENY"
  ! grep -qi '^strict-transport-security:' <<<"$headers" || fail "$path through Caddy without DOMAIN sends HSTS: $headers"
done
pass "/, /unit-roster, /favicon.svg, /api/health and /metrics through Caddy: the one policy, no framing, no HSTS without DOMAIN"

# The adapted configuration, as running (no DOMAIN) and as a DOMAIN installation's would be: the policy is set deferred, so it
# replaces any web sent rather than adding a second; HSTS is set, deferred too, only in the DOMAIN mode's snippet.
running=$(compose exec -T caddy caddy adapt --config /etc/caddy/Caddyfile 2>/dev/null | handlers headers)
domain=$(compose exec -T -e CADDY_SITE=roster.hospital.example -e CADDY_TLS_SNIPPET=self-signed-off caddy \
  caddy adapt --config /etc/caddy/Caddyfile 2>/dev/null | handlers headers)
python3 -c '
import json, sys
policy = sys.argv[1]
running, domain = ([handler["response"] for handler in json.loads(found)] for found in sys.argv[2:])
csp = {"deferred": True, "set": {"Content-Security-Policy": [policy]}}
hsts = {"deferred": True, "set": {"Strict-Transport-Security": ["max-age=31536000"]}}
assert running == [csp], running
assert sorted(map(json.dumps, domain)) == sorted(map(json.dumps, [csp, hsts])), domain' "$policy" "$running" "$domain" \
  && pass "the adapted configuration: the policy set deferred; HSTS max-age=31536000, deferred, with DOMAIN alone" \
  || fail "the adapted configuration's headers: running $running, with DOMAIN $domain"

# The audit log's address is the one Caddy worked out, whatever a client claims. With TRUSTED_PROXIES empty, as
# here, Caddy believes no X-Forwarded-For and replaces any X-Client-IP; gunicorn drops X_Client_IP, the same WSGI key.
claims="$work/claims"
# Logs the System Admin in through Caddy with the extra headers given, and prints the address its audit entry records.
recorded_login() {
  rm -f "$claims"
  curl -sk -c "$claims" -b "$claims" "$base/api/auth/session" >/dev/null
  local code
  code=$(curl -sk -o /dev/null -w '%{http_code}' -c "$claims" -b "$claims" -H "Origin: $base" -H 'Content-Type: application/json' \
    -H "X-CSRFToken: $(awk '$6 == "csrftoken" {print $7}' "$claims")" "$@" \
    -d "{\"username\": \"admin\", \"password\": \"$admin_password\"}" "$base/api/auth/login")
  [[ $code == 200 ]] || fail "the System Admin's login through Caddy answered $code"
  sql "SELECT coalesce(host(ip), 'none') FROM audit_auditentry WHERE action = 'auth.login' ORDER BY id DESC LIMIT 1"
}
peer=$(recorded_login)
claimed=$(recorded_login -H 'X-Forwarded-For: 203.0.113.7' -H 'X-Client-IP: 203.0.113.8' -H 'X_Client_IP: 203.0.113.9')
[[ $peer != none && $claimed == "$peer" ]] \
  && pass "a client's X-Forwarded-For, X-Client-IP and X_Client_IP do not reach the audit log, which has the address Caddy saw: $peer" \
  || fail "the audit log recorded $peer for a login that claimed nothing and $claimed for one that claimed addresses"

for service in web worker solver; do
  lines=$(compose logs --no-log-prefix --no-color "$service" | python3 -c '
import json, sys
lines = [line for line in sys.stdin if line.strip()]
for line in lines:
    json.loads(line)
print(len(lines))') || fail "$service logged a line that is not JSON"
  [[ $lines -gt 0 ]] && pass "$service: $lines lines, each one JSON" || fail "$service logged nothing"
done

made=$(compose logs --no-log-prefix --no-color web | python3 -c '
import json, sys
print(sum(1 for line in sys.stdin if line.strip() and (json.loads(line).get("logger"), json.loads(line).get("message"))
          == ("ops.prepare", "Made the installation\u0027s push key pair.")))')
[[ $made == 1 ]] && pass "prepare logged the push key pair it made at the first start, once" || fail "web logged the push key pair $made times"
[[ $(sql "SELECT count(*) FROM notifications_pushkeypair") == 1 ]] && pass "and the database holds one pair" || fail "no one push key pair"

# ---- gunicorn runs threaded --------------------------------------------------------------------------------------
# What the running gunicorn was given, read by gunicorn's own parser from its command line (PID 1, which `exec` made it) with
# config/gunicorn.py and any GUNICORN_CMD_ARGS, so no setting can hide in a file the command names: 3 workers of 4 threads,
# the timeout at gunicorn's own default, and a keep-alive of its own.
gunicorn_settings=$(compose exec -T web python -c '
import pathlib, sys
from gunicorn.app.wsgiapp import WSGIApplication
from gunicorn.config import Config
argv = pathlib.Path("/proc/1/cmdline").read_bytes().decode().split("\0")[:-1]
sys.argv = argv[next(i for i, arg in enumerate(argv) if arg.endswith("/gunicorn")):]
cfg = WSGIApplication().cfg
print(cfg.worker_class_str, cfg.workers, cfg.threads, cfg.timeout, Config().timeout, cfg.keepalive)') \
  || fail "web's PID 1 is not gunicorn, or its command line could not be read"
read -r worker_class workers threads gunicorn_timeout default_timeout keep_alive <<<"$gunicorn_settings"
[[ $worker_class == gthread && $workers == 3 && $threads == 4 && $gunicorn_timeout == "$default_timeout" && $keep_alive == 10 ]] \
  && pass "web's gunicorn runs $worker_class: $workers workers of $threads threads, the timeout at gunicorn's default of $gunicorn_timeout s, keep-alive $keep_alive s" \
  || fail "web's gunicorn: worker class $worker_class, $workers workers, $threads threads, timeout $gunicorn_timeout (default $default_timeout), keep-alive $keep_alive"
started=$(compose logs --no-log-prefix --no-color web | grep -c '"Using worker: gthread"' || true)
[[ $started == 1 ]] && pass "and says so as it starts: Using worker: gthread" || fail "web logged 'Using worker: gthread' $started times"
processes=$(compose exec -T web python -c '
import os, pathlib
print(sum(b"config.wsgi" in p.read_bytes() for p in pathlib.Path("/proc").glob("[0-9]*/cmdline") if p.parent.name != str(os.getpid())))') \
  || fail "web's processes could not be counted"
[[ $processes == 4 ]] && pass "web runs three gunicorn workers beside its master" || fail "web runs $processes gunicorn processes"

# A gthread worker keeps a connection open for --keep-alive seconds after a response and then closes it (checked once a
# second, so never sooner). Caddy forgets an idle connection to web before that, so it never sends a request down one gunicorn
# has just closed, which would answer a POST with 502.
caddy_idle=$(compose exec -T caddy caddy adapt --config /etc/caddy/Caddyfile 2>/dev/null | handlers reverse_proxy | python3 -c '
import json, sys
found = json.load(sys.stdin)
assert found
print(" ".join(str(p.get("transport", {}).get("keep_alive", {}).get("idle_timeout", "none")) for p in found))') \
  || fail "Caddy's adapted configuration has no reverse_proxy"
python3 -c 'import sys; idle = sys.argv[1].split(); sys.exit(not all(i.isdigit() and int(i) < float(sys.argv[2]) * 10**9 for i in idle))' "$caddy_idle" "$keep_alive" \
  && pass "Caddy keeps an idle connection to web $(( ${caddy_idle%% *} / 10**9 )) s, under gunicorn's keep-alive of $keep_alive s" \
  || fail "Caddy's idle time to web (ns): $caddy_idle, against gunicorn's keep-alive of $keep_alive s"
# Driven: POSTs through Caddy at gaps either side of Caddy's idle time and of gunicorn's keep-alive. Each is refused by web the
# same way, never with Caddy's 502. Go retries a GET on a reused connection that was closed, which is why these are POSTs.
answers=""
for gap in 0.5 1.5 2.5 $(( keep_alive - 1 )).5 $keep_alive.5; do
  sleep "$gap"
  answers="$answers $(curl -sk -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -d '{}' "$base/api/auth/login" || true)"
done
[[ $answers != *502* && $(tr ' ' '\n' <<<"$answers" | sort -u | grep -c .) == 1 ]] \
  && pass "POSTs through Caddy at gaps of half a second to $keep_alive.5 s are answered by web alike:$answers" \
  || fail "POSTs through Caddy at spaced gaps answered:$answers"

# ---- Caddy holds the body ----------------------------------------------------------------------------------------
# Caddy refuses a body over 2 MB (2,000,000 bytes) and reads the whole body before it opens the request to web, so no web
# worker waits on a client's body. After the log checks, since it recreates web, and web is put back before anything else.
version=$(compose exec -T caddy caddy version | awk '{print $1}')
python3 -c 'import re, sys; sys.exit(tuple(map(int, re.match(r"v(\d+)\.(\d+)\.(\d+)", sys.argv[1]).groups())) < (2, 11, 8))' "$version" \
  && pass "the running Caddy is $version: 2.11.8 or later, which refuses an oversized body from its Content-Length" \
  || fail "the running Caddy is $version, older than 2.11.8"
adapted=$(compose exec -T caddy caddy adapt --config /etc/caddy/Caddyfile 2>/dev/null)
python3 -c '
import json, sys
servers = json.loads(sys.argv[1])["apps"]["http"]["servers"]
assert servers
for name, server in servers.items():
    assert server.get("read_min_rate") == 10000 and server.get("read_idle_timeout") == 60 * 10**9, (name, server)
    assert not {"read_timeout", "write_timeout", "read_header_timeout", "idle_timeout"} & server.keys(), (name, server)' "$adapted" \
  && pass "the adapted configuration: a body that stalls for a minute or averages under 10 kB/s is dropped, and no whole-request deadline" \
  || fail "the adapted configuration's body timeouts: $adapted"
# A login of $1 bytes through Caddy; prints the answer's status. Django would refuse it too, but only once a worker had read it.
login_of() {
  head -c "$1" /dev/zero | tr '\0' x > "$work/body"
  curl -sk -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' --data-binary @"$work/body" "$base/api/auth/login"
}
over_cap=$(login_of 2000001) at_cap=$(login_of 2000000)
[[ $over_cap == 413 && $at_cap == 400 ]] && pass "a body of 2,000,001 bytes is 413 at Caddy, one of 2,000,000 reaches web, which refuses it: 400" \
  || fail "a body of 2,000,001 bytes answered $over_cap and one of 2,000,000 $at_cap"

# With web at one worker of one thread (tests/compose.one-worker.yaml), a login whose body never comes would leave nothing to
# answer anyone if Caddy passed it on: with twelve threads one held request never shows. One connection, as the audit's.
one_worker_web() { COMPOSE_FILE=$COMPOSE_FILE:tests/compose.one-worker.yaml docker compose up -d --no-deps --wait --quiet-pull web; }
one_worker_web
workers=$(compose exec -T web python -c '
import os, pathlib
print(sum(b"config.wsgi" in p.read_bytes() for p in pathlib.Path("/proc").glob("[0-9]*/cmdline") if p.parent.name != str(os.getpid())))')
[[ $workers == 2 ]] && pass "web runs one gunicorn worker beside its master for this check" || fail "web runs $workers gunicorn processes"
wait_for 60 curl -sfk "$base/api/health" || fail "no answer through Caddy from web at one worker"
# Sends a login's headers and, as $1 says, no body of its Content-Length, or 2,000,000 bytes of a chunked body (the most the
# cap lets through) without its last chunk; then holds the connection open.
withhold() {
  python3 -c '
import socket, ssl, sys, time
port, how = int(sys.argv[1]), sys.argv[2]
context = ssl.create_default_context()
context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
connection = context.wrap_socket(socket.create_connection(("127.0.0.1", port)), server_hostname="localhost")
head = b"POST /api/auth/login HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
if how == "length":
    connection.sendall(head + b"Content-Length: 64\r\n\r\n")
else:
    cap, size = 2_000_000, 10_000
    connection.sendall(head + b"Transfer-Encoding: chunked\r\n\r\n")
    for _ in range(cap // size):
        connection.sendall(b"%x\r\n" % size + b"x" * size + b"\r\n")
print("withheld", flush=True)
time.sleep(60)' "$port" "$1" > "$work/withheld" &
  holder=$!
  wait_for 30 grep -q withheld "$work/withheld" || fail "the login that withholds its body could not be sent"
}
# Django reads no chunked body, so a chunked login passed on would be answered at once: what shows it is web's access line.
for how in length chunked; do
  since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  withhold "$how"
  sleep 2  # what a request Caddy passed on would need to reach web's one worker, so the health request queues behind it
  code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "$base/api/health" || true)
  reached=$(compose logs --since "$since" --no-log-prefix --no-color web | grep -c '/api/auth/login' || true)
  kill "$holder" 2>/dev/null || true; wait "$holder" 2>/dev/null || true
  [[ $code == 200 && $reached == 0 ]] && pass "a login withholding its body ($how) never reaches web, and /api/health answers through Caddy" \
    || fail "with a login withholding its body ($how), /api/health through Caddy answered '$code' in 10 seconds; web logged it $reached times"
done
compose up -d --no-deps --wait --quiet-pull web
wait_for 60 curl -sfk "$base/api/health" || fail "no answer through Caddy once web is back at its own workers"

# ---- only the worker has a way out -------------------------------------------------------------------------------------------
# What the compose file promises of this running stack: the worker is on a network that is not internal, for the
# push services, and web, the database and the solver are on none. Whether the hospital's firewall lets the worker out is the
# installer's to check. Read from the containers themselves and from Docker's own word on each network.
ways_out() {
  local container network found=""
  container=$(compose ps -q "$1")
  [[ -n $container ]] || { echo "no container"; return; }
  for network in $(docker inspect -f '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}} {{end}}' "$container"); do
    [[ $(docker network inspect -f '{{.Internal}}' "$network") == false ]] && found="$found$network "
  done
  echo "${found% }"
}
out=$(ways_out worker)
[[ $out == "${project}_egress" ]] && pass "the worker is on one network that is not internal: $out" || fail "the worker's ways out: '$out'"
for service in web db solver; do
  out=$(ways_out "$service")
  [[ -z $out ]] && pass "$service is on no network with a way out" || fail "$service is on a network that is not internal: $out"
done

# ---- the browser checks' clock is off --------------------------------------------------------------------------------------
# e2e/compose.e2e.yaml alone passes E2E_CLOCK_FILE, which moves the hospital's date (nurses.membership.today). No
# service of an installation's compose file is given it, so no .env can switch it on; and it has no URL of its own, so there is
# nothing for Caddy to pass. Here: the variable in no service of compose.yaml as an installation runs it, nor in this stack's
# web and worker, whose setting is off and whose date is the real one.
COMPOSE_FILE=compose.yaml compose config --format json | python3 -c '
import json, sys
config = json.load(sys.stdin)
given = [name for name, service in config["services"].items() if "E2E_CLOCK_FILE" in (service.get("environment") or {})]
sys.exit(f"E2E_CLOCK_FILE is passed to {given}" if given else 0)' \
  && pass "deploy/compose.yaml passes the browser checks' clock to no service" || fail "the browser checks' clock in deploy/compose.yaml"
for service in web worker; do
  clock=$(compose exec -T "$service" python manage.py shell -c "
from django.conf import settings
from django.utils import timezone
from nurses import membership
print(settings.E2E_CLOCK_FILE, membership.today() == timezone.localdate())" 2>/dev/null | tail -1)
  [[ $clock == "None True" ]] && pass "$service: the clock is off and the hospital's date is the real one" || fail "$service's clock: $clock"
done


# ---- a backup by the job -----------------------------------------------------------------------------------------------------
backups() { compose exec -T worker sh -c 'ls /backups | grep -E "^nurse-roster-[0-9]{8}T[0-9]{6}Z\.dump$" || true'; }
compose exec -T worker python manage.py queue defer --ignore-already-enqueued ops.tasks.backup '{"timestamp": 0}' >/dev/null
has_backup() { [[ -n $(backups) ]]; }
wait_for 120 has_backup || fail "the backup job wrote no backup"
backup=$(backups | tail -n 1)
job=$(sql "SELECT status || ' ' || lock FROM procrastinate_jobs WHERE task_name = 'ops.tasks.backup' AND status <> 'todo' ORDER BY id DESC LIMIT 1")
[[ $job == "succeeded backup" ]] && pass "the job took $backup under the lock 'backup'" || fail "the backup job: '$job'"
mode=$(compose exec -T worker stat -c '%a %U' "/backups/$backup")
[[ $mode == "600 app" ]] && pass "the backup is the app user's alone: $mode" || fail "the backup's mode: $mode"
compose exec -T worker cat "/backups/$backup" | compose exec -T db pg_restore --list >/dev/null \
  && pass "pg_restore reads it" || fail "pg_restore cannot read $backup"

# ---- the retention jobs ------------------------------------------------------------------------------------------------------
sql "INSERT INTO notifications_pushattempt (at, outcome) VALUES (now() - interval '8 days', 'unreachable'), (now() - interval '6 days', 'sent')" >/dev/null
# Failed-login counts: one a day old goes, a fresh one and one locked now stay; so does a current throttle window.
sql "INSERT INTO accounts_loginattempts (username, failures, locked_until, last_failure_at) VALUES
  ('day-old', 2, NULL, now() - interval '25 hours'), ('fresh', 2, NULL, now() - interval '1 hour'),
  ('locked', 5, now() + interval '2 days', now() - interval '25 hours'), ('from-before', 3, NULL, NULL);
  INSERT INTO accounts_addressfailures (address, window_start, failures) VALUES
  ('192.0.2.1', now() - interval '16 minutes', 3), ('192.0.2.2', now() - interval '1 minute', 3)" >/dev/null
for task in ops.tasks.remove_ended_jobs notifications.tasks.remove_old_notifications notifications.tasks.remove_old_push_attempts accounts.tasks.clear_expired_sessions accounts.tasks.remove_old_login_failures; do
  compose exec -T worker python manage.py queue defer --ignore-already-enqueued "$task" '{"timestamp": 0}' >/dev/null
  done_ok() { [[ $(sql "SELECT count(*) FROM procrastinate_jobs WHERE task_name = '$task' AND status = 'succeeded'") -ge 1 ]]; }
  wait_for 60 done_ok && pass "$task succeeded" || fail "$task did not succeed: $(sql "SELECT status FROM procrastinate_jobs WHERE task_name = '$task'")"
done
[[ $(sql "SELECT string_agg(outcome, ' ') FROM notifications_pushattempt") == sent ]] \
  && pass "a push attempt eight days old is gone and one six days old is kept" || fail "push attempts kept: $(sql "SELECT at, outcome FROM notifications_pushattempt")"
kept_names="SELECT string_agg(username, ' ' ORDER BY username) FROM accounts_loginattempts
  WHERE username IN ('day-old', 'fresh', 'locked', 'from-before')"
[[ $(sql "$kept_names") == "fresh locked" ]] \
  && pass "a failed-login count a day old or of unknown age is gone, a fresh one and a locked one are kept" \
  || fail "failed-login counts kept: $(sql "$kept_names")"
kept_windows="SELECT string_agg(address, ' ') FROM accounts_addressfailures WHERE address LIKE '192.0.2.%'"
[[ $(sql "$kept_windows") == 192.0.2.2 ]] \
  && pass "an ended throttle window is gone and the current one is kept" || fail "throttle windows kept: $(sql "$kept_windows")"
sql "DELETE FROM accounts_loginattempts WHERE username IN ('fresh', 'locked');
  DELETE FROM accounts_addressfailures WHERE address = '192.0.2.2'" >/dev/null

# ---- logging in --------------------------------------------------------------------------------------------------------------
jar="$work/cookies"
csrf() { awk '$6 == "csrftoken" {print $7}' "$jar"; }
# Logs in afresh as $2 (the System Admin by default) and prints the answer's status.
log_in() {
  rm -f "$jar"
  curl -sk -c "$jar" -b "$jar" "$base/api/auth/session" >/dev/null
  curl -sk -o /dev/null -w '%{http_code}' -c "$jar" -b "$jar" -H "X-CSRFToken: $(csrf)" -H "Origin: $base" \
    -H 'Content-Type: application/json' -d "{\"username\": \"${2:-admin}\", \"password\": \"$1\"}" "$base/api/auth/login"
}
# A POST of $2 to $1 as whoever logged in last; prints the answer's body, and fails unless it is a success.
post() {
  curl -skf -c "$jar" -b "$jar" -H "X-CSRFToken: $(csrf)" -H "Origin: $base" -H 'Content-Type: application/json' -d "$2" "$base$1"
}

# ---- a restore ---------------------------------------------------------------------------------------------------------------
sql "CREATE TABLE written_after_the_backup (id int)" >/dev/null
# A file that is no backup, named relative to another directory: refused by pg_restore, so nothing changes.
echo "not a backup" > "$work/not-a-backup.dump"
deploy=$PWD
if (cd "$work" && "$deploy/restore.sh" --yes not-a-backup.dump); then fail "a file that is no backup was restored"; fi
[[ -n $(sql "SELECT to_regclass('written_after_the_backup')") ]] && pass "a failed restore leaves the database as it was" \
  || fail "a failed restore changed the database"
[[ $(sql "SELECT datname FROM pg_database WHERE datname LIKE 'nurse_roster%' ORDER BY 1" | tr '\n' ' ') == "nurse_roster " ]] \
  && pass "and leaves no database behind" || fail "databases left: $(sql "SELECT datname FROM pg_database")"
wait_for 60 curl -sfk "$base/api/health" && pass "the application answers again after the failed restore" || fail "no answer"

before=$(backups | wc -l | tr -d ' ')
./restore.sh --yes "$backup"
[[ -z $(sql "SELECT to_regclass('written_after_the_backup')") ]] && pass "the restore took back what was written after the backup" \
  || fail "the table written after the backup is still there"
[[ $(sql "SELECT count(*) FROM accounts_user WHERE username = 'admin'") == 1 ]] && pass "what the backup held is there" \
  || fail "the System Admin is missing after the restore"
after=$(backups | wc -l | tr -d ' ')
[[ $after -eq $((before + 1)) ]] && pass "the database as it stood was backed up first ($before backups, then $after)" \
  || fail "no backup before the restore ($before, then $after)"
wait_for 60 curl -sfk "$base/api/health" && pass "the application answers again after the restore" || fail "no answer after the restore"
[[ $(sql "SELECT datname FROM pg_database WHERE datname LIKE 'nurse_roster%' ORDER BY 1" | tr '\n' ' ') == "nurse_roster " ]] \
  && pass "no database is left over" || fail "databases left: $(sql "SELECT datname FROM pg_database")"
# The backup job that was running as that backup was taken comes back as running, under a worker long gone.
last=$(sql "SELECT coalesce(max(id), 0) FROM procrastinate_jobs")
compose exec -T worker python manage.py queue defer --ignore-already-enqueued solves.tasks.recover_lost_jobs '{"timestamp": 1}' >/dev/null
recovered() { [[ -n $(sql "SELECT 1 FROM procrastinate_jobs WHERE id > $last AND task_name = 'solves.tasks.recover_lost_jobs' AND status = 'succeeded'") ]]; }
wait_for 60 recovered && pass "recover_lost_jobs succeeds after the restore" || fail "recover_lost_jobs after the restore: $(sql "SELECT status FROM procrastinate_jobs WHERE id > $last")"
no_backup_stuck() { [[ -z $(sql "SELECT 1 FROM procrastinate_jobs WHERE task_name = 'ops.tasks.backup' AND status IN ('todo', 'doing')") ]]; }
wait_for 60 no_backup_stuck && pass "no backup job is left running or waiting behind the restored one" \
  || fail "backup jobs: $(sql "SELECT id || ' ' || status FROM procrastinate_jobs WHERE task_name = 'ops.tasks.backup'")"

# ---- what a restore empties and lists ----------------------------------------------------------------------------
# The job's backup above can be one the worker took at its first start, before anything here, so this backup is taken by hand.
# It holds a unit manager, to be deactivated after it; the System Admin locked out by five wrong passwords; and this address
# one failure short of its throttle (50 failed logins in its window, accounts.login.MAX_ADDRESS_FAILURES): the five, the
# locked refusal that shows the lockout (which counts too), and 43 at a name that is no account.
[[ $(log_in "$admin_password") == 200 ]] || fail "the System Admin cannot log in"
first_password=$admin_password admin_password=$(openssl rand -hex 12)  # the first start's must be changed before anything
post /api/auth/change-password "{\"current_password\": \"$first_password\", \"new_password\": \"$admin_password\"}" >/dev/null \
  || fail "the first password change was refused"
manager=$(post /api/users '{"username": "manager.after", "full_name": "Manager After", "role": "unit_manager"}' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["user"]["id"])') || fail "the unit manager was not made"
for _ in 1 2 3 4 5; do log_in not-the-password >/dev/null; done
[[ $(log_in "$admin_password") == 423 ]] || fail "five wrong passwords did not lock the System Admin out"
for _ in $(seq 43); do log_in not-the-password no.such.name >/dev/null; done
pass "the System Admin is locked out and this address one failed login short of its throttle"
locked=$(compose exec -T worker python manage.py backup | tail -n 1)
# After it, one of each change a restore undoes and lists: the server's reset lifts the System Admin's lockout, the reset's
# password is changed, the unit manager deactivated and the push keys rotated. The hospital's clock is read around them, so
# the times the restore prints are checked against a clock of the test's own.
zone=$(compose exec -T web printenv TIME_ZONE)
hospital_minute() { TZ=$zone date '+%Y-%m-%d %H:%M'; }
from=$(hospital_minute)
reset=$(./reset-admin-password.sh | sed -n 's/^New password for admin: //p')
[[ $(log_in "$reset") == 200 ]] || fail "the reset's password does not log in"
changed=$(openssl rand -hex 12)
post /api/auth/change-password "{\"current_password\": \"$reset\", \"new_password\": \"$changed\"}" >/dev/null \
  || fail "the password change was refused"
post "/api/users/$manager/deactivate" '{}' >/dev/null || fail "the deactivation was refused"
compose exec -T web python manage.py rotate_push_keys >/dev/null
to=$(hospital_minute)
# What a restore printed of what it undid, one entry a line in single spaces, and its closing line.
undone() { sed -n '/^Listing what the restore undid/,$p' <<<"$1" | sed '1d' | sed '/^Starting web and worker/,$d' | awk '{$1 = $1} 1'; }
closing="Recorded after the backup and undone by this restore: do these again by hand."
# Whether $1, what a restore printed, lists exactly the entries after it ("<action> <user>", in order), each at a minute from
# $from to $to on the hospital's clock, and then the closing line.
lists() {
  local shown entries at
  shown=$(undone "$1"); shift
  [[ $(tail -n 1 <<<"$shown") == "$closing" ]] || return 1
  entries=$(sed '$d' <<<"$shown")
  [[ $(awk '{print $1, $2}' <<<"$entries") == "$(printf '%s\n' "$@")" ]] || return 1
  while read -r at; do
    [[ ! $at < $from && ! $at > $to ]] || return 1
  done < <(awk '{print $3, $4}' <<<"$entries")
}
newest=$(sql "SELECT max(id) FROM audit_auditentry")
printed=$(./restore.sh --yes "$locked") || fail "the restore failed: $printed"
echo "$printed"
lists "$printed" "user.password_reset admin" "auth.password_changed admin" "user.deactivated manager.after" "push.keys_rotated -" \
  && pass "the restore listed the reset, the change, the deactivation and the key rotation it undid, by action, user and time" \
  || fail "the restore listed: $(undone "$printed")"
wait_for 60 curl -sfk "$base/api/health" || fail "no answer after restoring $locked"
failed=$(log_in not-the-password another.name) admin=$(log_in "$admin_password")
[[ $failed == 401 && $admin == 200 ]] && pass "the restore brought back no lockout and no throttle: a failed login, then the System Admin's" \
  || fail "after the restore a failed login answered $failed and the System Admin's $admin (423 a lockout, 429 the throttle)"

# Restoring the backup the restore took undoes it. What was done in between has ids below that backup's newest entry, so only
# a list by time names it.
taken_before=$(sed -n 's/^Restored from .*The database as it stood before is the backup \(.*\)\.$/\1/p' <<<"$printed")
[[ -n $taken_before ]] || fail "the restore named no backup of the database as it stood"
from=$(hospital_minute)
compose exec -T web python manage.py rotate_push_keys >/dev/null
to=$(hospital_minute)
[[ $(sql "SELECT max(id) FROM audit_auditentry WHERE action = 'push.keys_rotated'") -lt $newest ]] \
  || fail "the rotation after the restore has an id above the undone database's newest, so this proves nothing"
printed=$(./restore.sh --yes "$taken_before") || fail "restoring $taken_before failed: $printed"
echo "$printed"
lists "$printed" "push.keys_rotated -" \
  && pass "restoring the backup taken before the restore lists, by time, the rotation done in between" \
  || fail "restoring $taken_before listed: $(undone "$printed")"
admin_password=$changed  # what that backup holds, the lockout lifted by the reset
wait_for 60 curl -sfk "$base/api/health" || fail "no answer after restoring $taken_before"
admin=$(log_in "$admin_password")
[[ $admin == 200 ]] && pass "and the System Admin logs in with the password changed before it" \
  || fail "the System Admin's login after restoring $taken_before: $admin"

# A backup from before the throttle has no table of it, nor the time of a name's last failure: one made
# so restores, and `web` makes both, empty, as it migrates. Nothing is done after it, so the restore says there is nothing to do
# again.
sql "DROP TABLE accounts_addressfailures; ALTER TABLE accounts_loginattempts DROP COLUMN last_failure_at;
  DELETE FROM django_migrations WHERE app = 'accounts' AND name IN ('0003_address_failures', '0004_login_attempts_last_failure')" >/dev/null
without_throttle=$(compose exec -T worker python manage.py backup | tail -n 1)
printed=$(./restore.sh --yes "$without_throttle") || fail "restoring a backup without the throttle's table failed: $printed"
echo "$printed"
[[ $(undone "$printed") == "No deactivation, password reset, password change or push key rotation was recorded after the backup." ]] \
  && pass "a restore with nothing to list says so" || fail "a restore with nothing to list printed $(undone "$printed")"
wait_for 60 curl -sfk "$base/api/health" || fail "no answer after restoring a backup without the throttle's table"
failed=$(log_in not-the-password another.name) admin=$(log_in "$admin_password")
[[ $failed == 401 && $admin == 200 ]] \
  && pass "a backup without the throttle's table restores, and a failed login after it counts in the table web made" \
  || fail "after restoring a backup without the throttle's table a failed login answered $failed and the System Admin's $admin"

# The list is advice: a restore whose list cannot be read still finishes. Here the audit log of the database it replaces is
# not where the list looks.
sql "ALTER TABLE audit_auditentry RENAME TO audit_set_aside" >/dev/null
printed=$(./restore.sh --yes "$without_throttle" 2>&1) || fail "a restore whose list could not be printed failed: $printed"
grep -q '^Could not list what the restore undid' <<<"$printed" && pass "a restore says when it cannot list what it undid" \
  || fail "a restore whose list could not be printed said: $printed"
wait_for 60 curl -sfk "$base/api/health" && [[ $(log_in "$admin_password") == 200 ]] \
  && pass "and finishes all the same: the application answers and the System Admin logs in" \
  || fail "the application after a restore whose list could not be printed"

# ---- the admin reset lifts a lock --------------------------------------------------------------------------------------------
[[ $(log_in "$admin_password") == 200 ]] && pass "the System Admin logs in" || fail "the System Admin cannot log in"
for _ in 1 2 3 4 5; do log_in not-the-password >/dev/null; done
[[ $(log_in "$admin_password") == 423 ]] && pass "five wrong passwords lock the account, the right one included" || fail "no lock"
printed=$(./reset-admin-password.sh)
new_password=$(sed -n 's/^New password for admin: //p' <<<"$printed")
[[ -n $new_password ]] || fail "the reset printed no password: $printed"
[[ $(log_in "$new_password") == 200 ]] && pass "the reset script lifted the lock and its password logs in" || fail "still refused"
[[ $(log_in "$admin_password") == 401 ]] && pass "the old password no longer does" || fail "the old password still works"

# ---- the health answer and /metrics count pushes ----------------------------------------------------------------------------
# The System Admin's health answer is closed until a forced password is changed, so the reset's is changed first. Then three
# sends are scripted as rows, as the worker writes one a send: the stack's worker may reach no push service on purpose.
[[ $(log_in "$new_password") == 200 ]] || fail "the System Admin cannot log in to read the health answer"
token=$(awk '$6 == "csrftoken" {print $7}' "$jar")
code=$(curl -sk -o /dev/null -w '%{http_code}' -c "$jar" -b "$jar" -H "X-CSRFToken: $token" -H "Origin: $base" \
  -H 'Content-Type: application/json' -d "{\"current_password\": \"$new_password\", \"new_password\": \"$(openssl rand -hex 12)\"}" \
  "$base/api/auth/change-password")
[[ $code == 200 ]] || fail "the forced password change answered $code"
push_block() { curl -sk -b "$jar" "$base/api/ops/health" | python3 -c 'import json, sys; print(json.dumps(json.load(sys.stdin)["push"], sort_keys=True))'; }
block=$(push_block) || fail "the health answer carries no push block"
[[ $block == '{"failed": 0, "last_failure": null, "sent": 0}' ]] && pass "the health answer carries the push block, empty before any push: $block" \
  || fail "the push block before any push: $block"
sql "INSERT INTO notifications_pushattempt (at, outcome) VALUES (now() - interval '2 hours', 'sent'), (now() - interval '1 hour', 'gone'), (now(), 'unreachable'), (now() - interval '25 hours', 'refused')" >/dev/null
block=$(push_block) || fail "the health answer lost its push block"
[[ $block == '{"failed": 2, "last_failure": "unreachable", "sent": 1}' ]] && pass "and counts the last 24 hours' attempts with the last failure's reason: $block" \
  || fail "the push block after three sends in the day: $block"
gauges=$(compose exec -T caddy wget -qO- http://web:8000/metrics | { grep '^nurse_roster_push_attempts{' || true; } | tr '\n' ' ')
[[ $gauges == 'nurse_roster_push_attempts{result="sent"} 1 nurse_roster_push_attempts{result="failed"} 2 ' ]] \
  && pass "/metrics carries the same two counts" || fail "/metrics on pushes: $gauges"

# ---- a nurse import of the largest file, through Caddy -----------------------------------------------------------
# Caddy's 2 MB cap sits above the import's 1 MiB file limit (nurses.importing.MAX_BYTES) and the multipart around it. The file
# is three nurses and blank lines, which the import skips, to exactly that limit: each nurse costs a password hash to confirm.
cno_password=$(post /api/users '{"username": "cno.import", "full_name": "Import CNO", "role": "cno"}' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["generated_password"])') || fail "the CNO was not made"
[[ $(log_in "$cno_password" cno.import) == 200 ]] || fail "the CNO cannot log in"
post /api/auth/change-password "{\"current_password\": \"$cno_password\", \"new_password\": \"$(openssl rand -hex 12)\"}" >/dev/null \
  || fail "the CNO's first password change was refused"
post /api/units '{"name": "Import Ward", "manager_id": null}' >/dev/null || fail "the unit was not made"
python3 - "$work/import.csv" <<'PY'
import datetime, sys
rows = "Hospital ID,Full name,Gender,Unit,Start date\n" + "".join(
    f"N70-{n},Import Nurse {n},F,Import Ward,{datetime.date.today().isoformat()}\n" for n in (1, 2, 3))
with open(sys.argv[1], "w", newline="") as file:
    file.write(rows + "\n" * (1024 * 1024 - len(rows.encode())))
PY
[[ $(wc -c < "$work/import.csv" | tr -d ' ') == 1048576 ]] || fail "the import file is not 1 MiB"
upload() { curl -sk -c "$jar" -b "$jar" -H "X-CSRFToken: $(csrf)" -H "Origin: $base" -F "file=@$work/import.csv;type=text/csv" "$@"; }
preview_token=$(upload -f "$base/api/nurses/import/preview" | python3 -c 'import json, sys; print(json.load(sys.stdin)["token"] or "")') \
  || fail "the preview of a 1 MiB file through Caddy failed"
[[ -n $preview_token ]] && pass "a 1 MiB nurse import file previews through Caddy, with no problem" || fail "the 1 MiB file's preview has problems"
code=$(upload -o "$work/sheet" -w '%{http_code}' -F "token=$preview_token" "$base/api/nurses/import/confirm")
saved=$(python3 -c 'import json, sys; print(len(json.load(open(sys.argv[1]))["rows"]))' "$work/sheet" 2>/dev/null || echo none)
[[ $code == 201 && $saved == 3 ]] && pass "and confirms: 201, three nurses on the password sheet" || fail "the 1 MiB file's confirm answered $code, rows: $saved"

# ---- a confirmation longer than gunicorn's timeout ---------------------------------------------------------------
# Under gthread, gunicorn's timeout is only how long a worker's main loop may go silent: a request may take as long as it takes.
# With web held to one CPU, 300 nurses' password hashes keep one confirmation running past it, and it ends with its password
# sheet. 100 were too few: GitHub's runners hashed them in 27 to 29 s, inside the timeout, so the check showed nothing there
# (2026-11-12); 300 take about 80 s on those runners and minutes on a slower machine, still within MAX_ROWS (500). Last, since
# web stays at one CPU.
docker update --cpus 1 "$(compose ps -q web)" >/dev/null || fail "web could not be held to one CPU"
python3 - "$work/import.csv" <<'PY'
import datetime, sys
with open(sys.argv[1], "w", newline="") as file:
    file.write("Hospital ID,Full name,Gender,Unit,Start date\n" + "".join(
        f"N71-{n},Long Nurse {n},F,Import Ward,{datetime.date.today().isoformat()}\n" for n in range(1, 301)))
PY
preview_token=$(upload -f "$base/api/nurses/import/preview" | python3 -c 'import json, sys; print(json.load(sys.stdin)["token"] or "")') \
  || fail "the preview of 300 nurses failed"
[[ -n $preview_token ]] || fail "the preview of 300 nurses has problems"
rm -f "$work/sheet"
confirmed=$(upload -o "$work/sheet" -w '%{http_code} %{time_total}' -F "token=$preview_token" "$base/api/nurses/import/confirm") || true
read -r code seconds <<<"$confirmed"
seconds=${seconds:-0}
saved=$(python3 -c 'import json, sys; print(len(json.load(open(sys.argv[1]))["rows"]))' "$work/sheet" 2>/dev/null || echo none)
killed=$(compose logs --no-log-prefix --no-color web | grep -c 'WORKER TIMEOUT' || true)
[[ $code == 201 && $saved == 300 && $killed == 0 ]] \
  || fail "the long confirmation answered $code in $seconds s, rows: $saved; web logged WORKER TIMEOUT $killed times"
python3 -c 'import sys; sys.exit(float(sys.argv[1]) <= float(sys.argv[2]))' "$seconds" "$gunicorn_timeout" \
  && pass "a confirmation of 300 nurses on one CPU took $seconds s, past gunicorn's $gunicorn_timeout s, and ended: 201, 300 passwords" \
  || fail "the confirmation of 300 nurses on one CPU took $seconds s, inside gunicorn's $gunicorn_timeout s: it shows nothing"

echo "# All checks passed."
