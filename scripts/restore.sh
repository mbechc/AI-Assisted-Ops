#!/usr/bin/env bash
# Restore a database dump created by backup.sh:  restore.sh backups/db-YYYYmmdd-HHMMSS.sql.gz
# The matching config-*.tgz (.env with FERNET_KEY) must be in place, otherwise stored credentials cannot be decrypted.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root

dump="${1:-}"
[ -f "$dump" ] || die "usage: $0 <db-dump.sql.gz>"

c_warn "This replaces the current database with $dump"
read -r -p "Continue? [y/N] " ans
case "$ans" in y|Y|yes) ;; *) die "aborted" ;; esac

compose stop api worker scheduler web
gunzip -c "$dump" | compose exec -T postgres psql -q -v ON_ERROR_STOP=1 -U "$(env_get POSTGRES_USER)" -d "$(env_get POSTGRES_DB)"
compose run --rm api migrate
compose up -d
c_ok "Database restored"
