#!/usr/bin/env bash
# A stack of its own for the browser checks, built from this checkout: its own compose project, secrets, volumes, image tag and
# ports. It never touches another project, and refuses the installation's own name.
#
#   e2e/stack.sh up <project> [--empty]  build, start, seed (e2e/seed.py, then e2e/sound_swap.py for ER), take a backup; writes
#                                        e2e/.stack/<project>.json for playwright.config.ts. With --empty nothing is seeded:
#                                        the System Admin `admin` still has INITIAL_ADMIN_PASSWORD and its change due, as on an
#                                        installation's first start (the journeys start there).
#   e2e/stack.sh reset <project>         an --empty stack's database emptied again and prepared as on a first start, the clock
#                                        put back; the backups stay. Refused on a seeded stack.
#   e2e/stack.sh clock <project> <days>  the hospital's date moved <days> from the real one (0 puts it back), for web and the
#                                        worker alike (E2E_CLOCK_FILE, e2e/compose.e2e.yaml)
#   e2e/stack.sh fire <project> <task>   a periodic job run now by the stack's own worker, once nothing of it is waiting or
#                                        running, and waited for: e.g. swaps.tasks.expire_swaps
#   e2e/stack.sh swap <project> <unit> <YYYY-MM> [<from YYYY-MM-DD>]
#                                        prints a sound two-cell swap on that approved month as it stands (e2e/sound_swap.py),
#                                        on the first day it finds from tomorrow, or from the date given if later
#   e2e/stack.sh cover <project> <unit> <YYYY-MM> [<from YYYY-MM-DD>] [<avoid id,id>]
#                                        prints a cover sick leave would open on that approved month as it stands, and a
#                                        colleague the app's judging lists for it (e2e/sound_cover.py), on the first day it
#                                        finds from tomorrow, or from the date given if later; nothing is kept
#   e2e/stack.sh rebuild <project>       build the image again from this checkout and restart on it, keeping the data
#   e2e/stack.sh redeploy <project>      a new deploy, as far as a browser can tell: a new marker in index.html and a changed
#                                        service worker, then `web` restarted
#   e2e/stack.sh down <project>          remove the containers, volumes, image and e2e/.stack/<project>.* (a journey's notebook
#                                        among them)
#
# E2E_PORT (default 18080) is Caddy over plain HTTP on 127.0.0.1, the checks' origin; E2E_HTTPS_PORT (default 18443) is Caddy
# over HTTPS, as a hospital reaches it.
# Needs Docker with Compose v2.24 or later, openssl and python3.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
usage="usage: $0 up|reset|clock|fire|swap|cover|rebuild|redeploy|down <project> [...]"
command=${1:?$usage}
project=${2:?$usage}
[[ $project != nurse-roster ]] || { echo "Refused: nurse-roster is the installation's own project." >&2; exit 2; }
[[ $project =~ ^[a-z0-9][a-z0-9_-]*$ ]] || { echo "Refused: a compose project name is lower case letters, digits, - and _." >&2; exit 2; }
state="$here/.stack"
env_file="$state/$project.env"
mkdir -p "$state"

compose() {
  COMPOSE_PROJECT_NAME=$project COMPOSE_FILE="$here/../deploy/compose.yaml:$here/compose.e2e.yaml" COMPOSE_ENV_FILES=$env_file \
    docker compose "$@"
}

needs_stack() {
  [[ -e $env_file ]] || { echo "Nothing of $project here: $0 up $project" >&2; exit 2; }
}

# A manage.py shell script from e2e/ run in web; prints the one line it marks with $2.
in_web() {
  local script=$1 marker=$2
  shift 2
  compose exec -T "$@" web python manage.py shell < "$here/$script" | sed -n "s/^$marker //p"
}

# Waits for a running service's own health check, up to two minutes, without `up`, which may recreate the container.
healthy() {
  local id
  id=$(compose ps -q "$1")
  for _ in $(seq 120); do
    [[ $(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null) == healthy ]] && return 0
    sleep 1
  done
  echo "$1 is not healthy after two minutes." >&2
  return 1
}

set_clock() {
  [[ $1 =~ ^-?[0-9]+$ ]] || { echo "The clock moves by a whole number of days." >&2; exit 2; }
  compose exec -T -u root web sh -c "printf '%s\n' '$1' > /e2e-clock/days"
}

case $command in
  up)
    [[ ! -e $env_file ]] || { echo "$project is already up (or was not taken down): $0 down $project" >&2; exit 2; }
    seeded=true
    [[ ${3:-} != --empty ]] || seeded=false
    port=${E2E_PORT:-18080}
    initial_password=$(openssl rand -hex 12)
    cat > "$env_file" <<EOF
SECRET_KEY=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 16)
SOLVER_TOKEN=$(openssl rand -hex 16)
INITIAL_ADMIN_PASSWORD=$initial_password
APP_IMAGE=$project-app
APP_VERSION=e2e
E2E_PORT=$port
E2E_HTTPS_PORT=${E2E_HTTPS_PORT:-18443}
EOF
    chmod 600 "$env_file"
    password=$(openssl rand -hex 12)
    echo "# Building and starting $project…"
    compose up -d --build --wait --quiet-pull
    # The volume is new and root's: the clock starts at the real date, as a file the app's user can read.
    compose exec -T -u root web sh -c "printf '0\n' > /e2e-clock/days && chmod 644 /e2e-clock/days"
    seed='{"names": {}}'
    if $seeded; then
      compose cp "$here/../solver/service_tests/fixtures/four_nurses.json" web:/tmp/four_nurses.json >/dev/null
      echo "# Seeding (four months solved for real, two of them of sixty-four nurses: a couple of minutes)…"
      seed=$(in_web seed.py E2E_SEED -e SEED_PASSWORD="$password")
      [[ -n $seed ]] || { echo "The seed printed nothing." >&2; exit 1; }
      month=$(python3 -c 'import json, sys; print(json.loads(sys.argv[1])["approved_month"])' "$seed")
      swap=$(in_web sound_swap.py E2E_SWAP -e UNIT=ER -e MONTH="$month")
      [[ -n $swap ]] || { echo "No sound swap was found." >&2; exit 1; }
      seed=$(python3 -c 'import json, sys; print(json.dumps({**json.loads(sys.argv[1]), "swap": json.loads(sys.argv[2])}))' "$seed" "$swap")
    fi
    # A backup at once, for the checks that it is never served from a cache, rather than within the worker's first five minutes.
    compose exec -T worker python manage.py backup >/dev/null
    python3 - "$state/$project.json" "$port" "${E2E_HTTPS_PORT:-18443}" "$password" "$initial_password" "$seeded" "$seed" <<'EOF'
import json, sys
path, port, https_port, password, initial_password, seeded, seed = sys.argv[1:]
json.dump({"baseURL": f"http://localhost:{port}", "caddyURL": f"https://localhost:{https_port}", "seeded": seeded == "true",
           "password": password, "initialAdminPassword": initial_password, **json.loads(seed)}, open(path, "w"), indent=2)
EOF
    chmod 600 "$state/$project.json"
    echo "# Up: e2e/.stack/$project.json. Run the checks with E2E_STACK=$project npx playwright test"
    ;;
  reset)
    needs_stack
    python3 -c 'import json, sys; sys.exit(0 if not json.load(open(sys.argv[1]))["seeded"] else 1)' "$state/$project.json" \
      || { echo "Refused: $project is seeded; only an --empty stack is reset." >&2; exit 2; }
    set_clock 0
    compose exec -T web python manage.py flush --no-input
    # As on a first start: INITIAL_ADMIN_PASSWORD is in the service's environment, which `exec` gets.
    compose exec -T web python manage.py bootstrap >/dev/null
    ;;
  clock)
    needs_stack
    set_clock "${3:?usage: $0 clock <project> <days>}"
    ;;
  fire)
    needs_stack
    task=${3:?usage: $0 fire <project> <module.tasks.job>}
    [[ $task =~ ^[a-z_]+\.tasks\.[a-z_]+$ ]] || { echo "A job is named module.tasks.job." >&2; exit 2; }
    # Each wait is on the queue's own rows: first for a run already waiting or under way to end, since it may have read the
    # clock before it moved and would take our copy's place; then for ours. At most two minutes each.
    wait_idle="from procrastinate.contrib.django.models import ProcrastinateJob as J
import time
for _ in range(240):
    if not J.objects.filter(task_name='$task', status__in=('todo', 'doing')).exists():
        break
    time.sleep(0.5)
else:
    raise SystemExit('$task is still on the queue after two minutes')"
    compose exec -T worker python manage.py shell -c "$wait_idle" >/dev/null
    compose exec -T worker python manage.py queue defer --ignore-already-enqueued "$task" '{"timestamp": 0}' >/dev/null
    compose exec -T worker python manage.py shell -c "$wait_idle" >/dev/null
    compose exec -T worker python manage.py shell -c "from procrastinate.contrib.django.models import ProcrastinateJob as J
latest = J.objects.filter(task_name='$task').order_by('-id').first()
raise SystemExit(0 if latest and latest.status == 'succeeded' else f'$task ended {latest and latest.status}')" >/dev/null
    ;;
  swap)
    needs_stack
    unit=${3:?usage: $0 swap <project> <unit> <YYYY-MM>}
    month=${4:?usage: $0 swap <project> <unit> <YYYY-MM>}
    in_web sound_swap.py E2E_SWAP -e UNIT="$unit" -e MONTH="$month" -e FROM="${5:-}"
    ;;
  cover)
    needs_stack
    unit=${3:?usage: $0 cover <project> <unit> <YYYY-MM>}
    month=${4:?usage: $0 cover <project> <unit> <YYYY-MM>}
    in_web sound_cover.py E2E_COVER -e UNIT="$unit" -e MONTH="$month" -e FROM="${5:-}" -e AVOID="${6:-}"
    ;;
  rebuild)
    needs_stack
    compose up -d --build --wait --quiet-pull
    ;;
  redeploy)
    needs_stack
    marker="$(date +%s)-$RANDOM"
    compose exec -T web sh -c "sed -i 's|<head>|<head><meta name=\"e2e-deploy\" content=\"$marker\">|' /app/spa/index.html \
      && printf '\n// e2e deploy %s\n' $marker >> /app/spa/sw.js"
    # A restart and a wait for its health check, never `up`: on GitHub's runners `up -d --wait web` recreated the container
    # (and the solver's) from the image, and the edited files were gone with it; the browser then got the old page.
    compose restart web >/dev/null
    healthy web
    echo "$marker"
    ;;
  down)
    [[ -e $env_file ]] || { echo "Nothing of $project here." >&2; exit 2; }
    compose down -v --remove-orphans
    docker image rm "$project-app:e2e" >/dev/null 2>&1 || true
    rm -f "$env_file" "$state/$project.json" "$state/$project".*.json
    ;;
  *)
    echo "$usage" >&2; exit 2 ;;
esac
