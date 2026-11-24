#!/usr/bin/env bash
# Stop and remove Steward containers. --purge also deletes volumes (database, model), images and $INSTALL_DIR.
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root

purge=0
[ "${1:-}" = "--purge" ] && purge=1

remove_updater() {
  [ -f /etc/systemd/system/steward-updater.path ] || return 0
  systemctl disable --now steward-updater.path >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/steward-updater.path /etc/systemd/system/steward-updater.service
  systemctl daemon-reload
}

if [ "$purge" = 1 ]; then
  c_warn "This deletes ALL Steward data (database, LLM model, configuration) in $INSTALL_DIR"
  read -r -p "Type 'purge' to continue: " ans
  [ "$ans" = "purge" ] || die "aborted"
  images="$(COMPOSE_PROFILES=mock,llm compose config --images 2>/dev/null | sort -u || true)"
  COMPOSE_PROFILES=mock,llm compose down -v --remove-orphans
  # shellcheck disable=SC2086
  [ -n "$images" ] && docker image rm $images >/dev/null 2>&1 || true
  remove_updater
  rm -f /etc/sysctl.d/90-steward.conf
  rm -rf "$INSTALL_DIR"
  c_ok "Steward removed completely"
else
  COMPOSE_PROFILES=mock,llm compose down --remove-orphans
  c_ok "Steward stopped and containers removed (data kept; start again with: docker compose up -d in $INSTALL_DIR)"
fi
