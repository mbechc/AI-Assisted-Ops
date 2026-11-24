#!/usr/bin/env bash
# Show Steward's terms (DISCLAIMER.md) and record their acceptance for this installation.
# Needed once on installations made before the installer asked for it.
#
#   sudo /opt/Steward/scripts/accept-terms.sh [--accept-terms]
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export INSTALL_DIR
VERSION="$(tr -d '[:space:]' <"$INSTALL_DIR/VERSION" 2>/dev/null || echo unknown)"
terms="$INSTALL_DIR/DISCLAIMER.md"
[ -f "$terms" ] || die "$terms is missing"

if terms_accepted; then
  c_ok "Terms already accepted on this installation:"
  cat "$INSTALL_DIR/$TERMS_RECORD_REL"
  exit 0
fi
if [ "${1:-}" = "--accept-terms" ]; then
  method=flag
elif have_tty; then
  terms_prompt "$terms" || die "terms not accepted"
  method=typed
else
  die "no terminal: run this on a terminal, or add --accept-terms"
fi
terms_record "$terms" "$method"
c_ok "Terms accepted and recorded in $INSTALL_DIR/$TERMS_RECORD_REL"
