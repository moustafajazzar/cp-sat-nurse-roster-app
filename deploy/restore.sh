#!/usr/bin/env bash
# Restores the database from a backup. Restore is deliberately a script on the server, not a button.
# Run it from anywhere; it works on the stack of the compose.yaml beside it, as `docker compose` does there.
#
#   ./restore.sh nurse-roster-20261105T230000Z.dump      a backup the server keeps (the names are on the health page)
#   ./restore.sh /home/it/copies/nurse-roster-....dump   a copy from elsewhere, an off-site one for instance
#
# What it does, in order:
#   1. stops `web` and `worker`, so nothing writes; Caddy stays up and answers 502 meanwhile;
#   2. takes a backup of the database as it stands, kept beside the others, so a wrong choice can be undone;
#   3. restores into a new database and empties its login attempts, the lockouts and the throttle's addresses, and only once
#      that has worked puts it in the old one's place;
#   4. lists, from the old one, the deactivations, password resets and changes and push key rotations it undid;
#   5. starts `web` and `worker` again: `web` migrates a backup from an older version forward.
# If anything fails or the script is stopped before step 3's swap is done, the database is as it was, and `web` and `worker`
# start again on it. The swap renames the old database aside, the restored one in, and drops the old one last.
# The list is advice: if it cannot be printed the restore still finishes, and the backup of step 2 holds the same entries.
#
# Options: --yes skips the question. The project is compose's own: COMPOSE_PROJECT_NAME, COMPOSE_FILE and
# COMPOSE_ENV_FILES are honoured, as `docker compose` honours them.
set -euo pipefail

usage() { echo "usage: $0 [--yes] <backup name or file>" >&2; exit 2; }
ask=1
case "${1:-}" in --yes) ask=0; shift ;; esac
[[ $# -eq 1 ]] || usage
backup=$1
# A file is read where the script was called from, before it moves to its own directory.
[[ -f $backup ]] && backup="$(cd "$(dirname "$backup")" && pwd)/$(basename "$backup")"
cd "$(dirname "$0")"

DB=nurse_roster
RESTORING=nurse_roster_restoring
REPLACED=nurse_roster_replaced
NAME='^nurse-roster-[0-9]{8}T[0-9]{6}Z\.dump$'

compose() { docker compose "$@"; }
# psql in the database $1; the rest are psql's own arguments.
psql_in() {
  local database=$1; shift
  compose exec -T -e PGOPTIONS='-c client_min_messages=warning' db \
    psql --username "$DB" --dbname "$database" --quiet --no-psqlrc --tuples-only --set ON_ERROR_STOP=1 "$@"
}
psql_db() { psql_in postgres "$@"; }
exists() { [[ -n $(psql_db -c "SELECT 1 FROM pg_database WHERE datname = '$1'") ]]; }
# One-off containers of the worker's image read the backups volume, whether or not the worker runs.
in_worker() { compose run --rm --no-deps -T worker "$@"; }

# Prints each deactivation, password reset, password change and push key rotation in $REPLACED later than the newest audit
# entry of $DB, the restored one: by time, not id, since restoring the backup a restore took gives back a database whose ids
# run past those written in between. Its action, its user ("-" for none) and its time on the hospital's clock, which is
# the database container's TZ (compose sets it from TIME_ZONE), then a line saying to do them again; or a line saying there
# were none. It reads everything before it prints anything.
list_undone() {
  local newest zone rows action user at
  newest=$(psql_in "$DB" --no-align -c "SELECT coalesce(max(at), '-infinity') FROM audit_auditentry") || return 1
  zone=$(compose exec -T db printenv TZ) || return 1
  rows=$(psql_in "$REPLACED" --no-align --field-separator=$'\t' -v newest="$newest" -v zone="$zone" <<'SQL'
SELECT action, coalesce(nullif(target, ''), '-'), to_char(at AT TIME ZONE :'zone', 'YYYY-MM-DD HH24:MI')
FROM audit_auditentry
WHERE action IN ('user.deactivated', 'user.password_reset', 'auth.password_changed', 'push.keys_rotated')
  AND at > :'newest'::timestamptz
ORDER BY at, id;
SQL
  ) || return 1
  if [[ -z $rows ]]; then
    echo "No deactivation, password reset, password change or push key rotation was recorded after the backup."
    return 0
  fi
  while IFS=$'\t' read -r action user at; do
    printf '  %-22s %-24s %s\n' "$action" "$user" "$at"
  done <<<"$rows"
  echo "Recorded after the backup and undone by this restore: do these again by hand."
}

if [[ -f $backup ]]; then
  source_of() { cat -- "$backup"; }
  what="the file $backup"
elif [[ $backup =~ $NAME ]]; then
  in_worker test -f "/backups/$backup" || { echo "No backup called $backup on this server. The health page lists them." >&2; exit 1; }
  source_of() { in_worker cat "/backups/$backup"; }
  what="the backup $backup"
else
  echo "$backup is neither a file nor the name of a backup (nurse-roster-<UTC time>.dump)." >&2
  exit 1
fi

compose ps --status running --services | grep -qx db || { echo "The database service is not running: start it first." >&2; exit 1; }
if exists "$REPLACED"; then
  echo "A database called $REPLACED is left from a restore that did not finish: it may be the only copy of the data." >&2
  echo "Read the comment at the top of deploy/restore.sh before going on." >&2
  exit 1
fi

if [[ $ask == 1 ]]; then
  echo "This replaces every row of the application's database with $what."
  echo "Everything done since that backup is lost; a backup of the database as it stands is taken first."
  read -r -p "Type RESTORE to go on: " answer
  [[ $answer == RESTORE ]] || { echo "Nothing was changed."; exit 1; }
fi

aside=0
swapped=0
restart() {
  status=$?
  trap - EXIT
  if [[ $swapped == 0 ]]; then
    if [[ $aside == 1 ]] && ! exists "$DB"; then
      psql_db -c "ALTER DATABASE $REPLACED RENAME TO $DB" \
        || echo "Could not put $REPLACED back as $DB: the data is in $REPLACED." >&2
    fi
    psql_db -c "DROP DATABASE IF EXISTS $RESTORING WITH (FORCE)" >/dev/null 2>&1 || true
    echo "The restore did not finish. The database is as it was; starting the application again." >&2
  fi
  compose up -d --no-deps web worker
  exit "$status"
}
trap restart EXIT
trap 'exit 130' INT HUP TERM

echo "Stopping web and worker…"
compose stop web worker

echo "Taking a backup of the database as it stands…"
before=$(in_worker python manage.py backup | tail -n 1)
echo "Kept as $before."

echo "Restoring $what into a new database…"
psql_db -c "DROP DATABASE IF EXISTS $RESTORING WITH (FORCE)" -c "CREATE DATABASE $RESTORING OWNER $DB"
source_of | compose exec -T db pg_restore --username "$DB" --dbname "$RESTORING" --no-owner --exit-on-error --single-transaction
# No lockout or throttle of the backup's moment comes back. A backup from before the throttle has no table of it: `web` makes that
# one, empty, when it migrates.
echo "Emptying its login attempts…"
psql_in "$RESTORING" <<'SQL'
DO $$
DECLARE login_table text;
BEGIN
  FOREACH login_table IN ARRAY ARRAY['accounts_loginattempts', 'accounts_addressfailures'] LOOP
    IF to_regclass('public.' || login_table) IS NOT NULL THEN
      EXECUTE format('TRUNCATE %I', login_table);
    END IF;
  END LOOP;
END
$$;
SQL

echo "Putting it in the old one's place…"
psql_db -c "ALTER DATABASE $DB RENAME TO $REPLACED"
aside=1
psql_db -c "ALTER DATABASE $RESTORING RENAME TO $DB"
swapped=1
# Stopped from here on, it still starts the application again: the restore is done.
echo "Listing what the restore undid…"
list_undone || echo "Could not list what the restore undid: the backup $before holds the audit log as it stood." >&2
psql_db -c "DROP DATABASE $REPLACED WITH (FORCE)" \
  || echo "The old database is left as $REPLACED: drop it once the restore is checked." >&2
trap - EXIT INT HUP TERM

echo "Starting web and worker…"
compose up -d --no-deps --wait web worker
echo "Restored from $what. The database as it stood before is the backup $before."
