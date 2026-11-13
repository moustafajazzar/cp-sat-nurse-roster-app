#!/usr/bin/env bash
# Gives the System Admin a new password from the server, for when it is lost or the account is locked by wrong passwords.
# The new password is printed once and must be changed at the next
# login; the account's sessions end, and a lock from failed logins is lifted.
#
#   ./reset-admin-password.sh                      the account the first start made, `admin`
#   ./reset-admin-password.sh --username it-lead   another System Admin account
#
# The project is compose's own: COMPOSE_PROJECT_NAME, COMPOSE_FILE and COMPOSE_ENV_FILES are honoured.
set -euo pipefail
cd "$(dirname "$0")"

if docker compose ps --status running --services | grep -qx web; then
  docker compose exec -T web python manage.py reset_admin_password "$@"
else
  docker compose run --rm --no-deps -T web python manage.py reset_admin_password "$@"
fi
