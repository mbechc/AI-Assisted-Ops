#!/usr/bin/env bash
# Upgrade an existing installation from this (newer) package: backup first, then re-run the
# idempotent installer, which keeps configuration, certificates, data, model and users.
# Accepts the same options as install.sh.
set -euo pipefail
# The shell may still be in a folder that no longer exists (for example after uninstall.sh --purge).
pwd -P >/dev/null 2>&1 || cd /
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SRC_DIR/scripts/lib.sh"
require_root

args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  [ "${args[$i]}" = "--install-dir" ] && INSTALL_DIR="${args[$((i + 1))]:-$INSTALL_DIR}"
done
export INSTALL_DIR
[ -f "$INSTALL_DIR/.env" ] || die "no installation found in $INSTALL_DIR (use install.sh)"

if [ -n "$(compose ps -q --status running postgres 2>/dev/null)" ]; then
  c_info "Backing up current installation"
  "$INSTALL_DIR/scripts/backup.sh"
else
  c_warn "Database is not running (earlier install did not finish?): no backup taken"
fi
exec "$SRC_DIR/install.sh" --yes "$@"
