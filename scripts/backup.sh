#!/usr/bin/env bash
# Database dump + configuration (secrets, certificates) into $INSTALL_DIR/backups. Keeps the last 14.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root

ts="$(date +%Y%m%d-%H%M%S)"
dir="$INSTALL_DIR/backups"
mkdir -p "$dir"
chmod 700 "$dir"
umask 077

compose exec -T postgres pg_dump -U "$(env_get POSTGRES_USER)" -d "$(env_get POSTGRES_DB)" --clean --if-exists \
  | gzip >"$dir/db-$ts.sql.gz"
tar -czf "$dir/config-$ts.tgz" -C "$INSTALL_DIR" .env certs
c_ok "Backup written: $dir/db-$ts.sql.gz and $dir/config-$ts.tgz"

ls -1t "$dir"/db-*.sql.gz 2>/dev/null | tail -n +15 | xargs -r rm -f
ls -1t "$dir"/config-*.tgz 2>/dev/null | tail -n +15 | xargs -r rm -f
