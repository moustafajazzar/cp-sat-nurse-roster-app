#!/usr/bin/env bash
# The browser checks and the compose-level test as CI runs them (.github/workflows/browser-checks.yml), and as anyone can run
# them the same way from a checkout: one image built from it, a seeded stack and an empty one started on it, every Playwright
# project against them, deploy/tests/operations.sh on a stack of its own, then everything removed. It fails if anything fails;
# what failed is in e2e/test-results (Playwright's traces) and the output.
#
#   e2e/ci.sh
#
# E2E_CI_PREFIX (default nr-ci) names its three compose projects, <prefix>-seeded, <prefix>-journey and <prefix>-ops; the ports
# are 18180–18183 and 18543–18545. Needs Docker with Compose v2.24 or later, Node 26, curl, openssl and python3; CI also
# installs what Chromium needs from the system (`npx playwright install-deps chromium`).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
prefix=${E2E_CI_PREFIX:-nr-ci}
seeded=$prefix-seeded
journey=$prefix-journey
ops=$prefix-ops

down() {
  for project in "$seeded" "$journey"; do
    [[ ! -e $here/.stack/$project.env ]] || "$here/stack.sh" down "$project" >/dev/null 2>&1 || true
  done
}
trap down EXIT

started=$(date +%s)
step() { echo; echo "## $* ($(( $(date +%s) - started )) s in)"; }

step "The checks' own dependencies, and their typecheck"
(cd "$here" && npm ci --no-audit --no-fund && npx playwright install chromium && npm run typecheck)

step "The image, and a seeded stack on it"
E2E_PORT=18180 E2E_HTTPS_PORT=18543 "$here/stack.sh" up "$seeded"
step "An empty stack on the same image's layers, for the journeys"
E2E_PORT=18181 E2E_HTTPS_PORT=18544 "$here/stack.sh" up "$journey" --empty

step "Every Playwright project"
failed=0
(cd "$here" && CI=1 E2E_STACK=$seeded E2E_JOURNEY_STACK=$journey npx playwright test) || failed=1
down

step "deploy/tests/operations.sh"
TEST_HTTPS_PORT=18545 "$here/../deploy/tests/operations.sh" "$ops" || failed=1

step "Done: $([[ $failed == 0 ]] && echo "every check passed" || echo "FAILED, see above")"
exit $failed
