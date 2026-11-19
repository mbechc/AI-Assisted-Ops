#!/usr/bin/env bash
# End-to-end acceptance test against the simulated Catalyst Centers (requires install --with-mock).
#   acceptance-mock.sh [--password PW] [--llm-items N] [--approve N]
# Uses the admin password from initial-admin-password.txt when --password is not given.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root

args=("$@")
if [[ " $* " != *" --password "* ]]; then
  pwfile="$INSTALL_DIR/initial-admin-password.txt"
  [ -r "$pwfile" ] || die "pass --password (no $pwfile)"
  args+=(--password "$(sed -n 2p "$pwfile")")
fi
profile_has mock || die "simulators not enabled; re-run install.sh --with-mock"
compose exec -T api python -m app.tools.e2e_mock "${args[@]}"
