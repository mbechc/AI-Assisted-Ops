#!/usr/bin/env bash
# Stop and remove AIOps containers. --purge also deletes volumes (database, model), images and $INSTALL_DIR.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root

purge=0
[ "${1:-}" = "--purge" ] && purge=1

remove_updater() {
  [ -f /etc/systemd/system/aiops-updater.path ] || return 0
  systemctl disable --now aiops-updater.path >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/aiops-updater.path /etc/systemd/system/aiops-updater.service
  systemctl daemon-reload
}

if [ "$purge" = 1 ]; then
  c_warn "This deletes ALL AIOps data (database, LLM model, configuration) in $INSTALL_DIR"
  read -r -p "Type 'purge' to continue: " ans
  [ "$ans" = "purge" ] || die "aborted"
  images="$(COMPOSE_PROFILES=mock compose config --images 2>/dev/null | sort -u || true)"
  COMPOSE_PROFILES=mock compose down -v --remove-orphans
  # shellcheck disable=SC2086
  [ -n "$images" ] && docker image rm $images >/dev/null 2>&1 || true
  remove_updater
  rm -rf "$INSTALL_DIR"
  c_ok "AIOps removed completely"
else
  COMPOSE_PROFILES=mock compose down --remove-orphans
  c_ok "AIOps stopped and containers removed (data kept; start again with: docker compose up -d in $INSTALL_DIR)"
fi
