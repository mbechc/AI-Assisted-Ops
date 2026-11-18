#!/usr/bin/env bash
# Record sanitized API responses from a configured Catalyst Center source into data/fixtures/<release>/.
#   record-fixtures.sh <source-id>      (see the id on the Data sources page)
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root
[ -n "${1:-}" ] || die "usage: $0 <source-id>"
compose exec -T api python -m app.manage record-fixtures --source-id "$1" --out /data/fixtures
c_ok "Fixtures in $INSTALL_DIR/data/fixtures"
