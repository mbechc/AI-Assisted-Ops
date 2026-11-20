#!/usr/bin/env bash
# Restore a database dump created by backup.sh:  restore.sh [--no-migrate] backups/db-YYYYmmdd-HHMMSS.sql.gz
# The matching config-*.tgz (.env with FERNET_KEY) must be in place, otherwise stored credentials cannot be decrypted.
# --no-migrate (rollback): leave the schema as in the dump and the application stopped; then install the
# release the dump came from (get-aiops.sh --version YYYY.M.R), which migrates and starts it.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root

migrate=1
if [ "${1:-}" = "--no-migrate" ]; then migrate=0; shift; fi
dump="${1:-}"
[ -f "$dump" ] || die "usage: $0 [--no-migrate] <db-dump.sql.gz>"

c_warn "This replaces the current database with $dump"
read -r -p "Continue? [y/N] " ans
case "$ans" in y|Y|yes) ;; *) die "aborted" ;; esac

app="$(services | grep -vxE 'postgres|redis|ollama|logproxy' | tr '\n' ' ')"
# shellcheck disable=SC2086
compose stop $app
gunzip -c "$dump" | compose exec -T postgres psql -q -v ON_ERROR_STOP=1 -U "$(env_get POSTGRES_USER)" -d "$(env_get POSTGRES_DB)"
if [ "$migrate" = 0 ]; then
  c_ok "Database restored; application stopped. Now install the matching release."
  exit 0
fi
compose run --rm -T api migrate
compose up -d
c_ok "Database restored"
