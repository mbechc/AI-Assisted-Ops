#!/usr/bin/env bash
# AIOps one-line installer: finds the release for the chosen track on GitHub, verifies its checksum
# and runs install.sh (or upgrade.sh when AIOps is already installed in the target folder).
#
#   curl -fsSL https://raw.githubusercontent.com/mbechc/AI-Assisted-Ops/master/get-aiops.sh | sudo bash
#   curl -fsSL .../get-aiops.sh | sudo bash -s -- --channel development --with-mock
#
# Behind a proxy, export https_proxy and run with sudo -E.
set -euo pipefail

REPO="${AIOPS_REPO:-mbechc/AI-Assisted-Ops}"
DIR=/opt/AIOps
CHANNEL=production
VERSION=""
PASS=()

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: get-aiops.sh [options] [install.sh options]

  --dir DIR          Installation folder (default /opt/AIOps)
  --channel TRACK    production (default, stable releases) or development (also pre-releases)
  --version X.Y.Z    Install this exact release instead of the newest on the track
  -h, --help         This help

Any other option is passed to install.sh, for example --with-mock, --hostname NAME,
--admin-password PW, --https-port N.
EOF
}

# Everything runs from main, so bash has read the whole script before it starts (curl | bash).
main() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --dir) DIR="$2"; shift 2 ;;
      --channel)
        case "${2:-}" in production|development) CHANNEL="$2" ;; *) die "--channel must be production or development" ;; esac
        shift 2 ;;
      --version) VERSION="${2#v}"; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) PASS+=("$1"); shift ;;
    esac
  done

  [ "$(id -u)" -eq 0 ] || die "run as root: curl -fsSL <url> | sudo bash"
  [ "$(uname -s)" = Linux ] || die "Linux is required"
  [ "$(uname -m)" = x86_64 ] || die "x86_64 is required (found $(uname -m))"
  [ -r /etc/os-release ] || die "cannot detect the OS (/etc/os-release missing)"
  for c in curl tar sha256sum; do command -v "$c" >/dev/null 2>&1 || die "$c is required"; done

  api() { curl -fsSL --retry 3 --connect-timeout 15 -H "Accept: application/vnd.github+json" "https://api.github.com/repos/$REPO/$1"; }
  tag_of() { grep -m1 '"tag_name"' | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/'; }

  if [ -n "$VERSION" ]; then
    TAG="v$VERSION"
  elif [ "$CHANNEL" = production ]; then
    json="$(api releases/latest)" || die "cannot reach api.github.com, or no Production release is published yet"
    TAG="$(tag_of <<<"$json" || true)"
  else
    # Newest first; includes pre-releases.
    json="$(api "releases?per_page=10")" || die "cannot reach api.github.com"
    TAG="$(tag_of <<<"$json" || true)"
  fi
  [ -n "$TAG" ] || die "no release found for track $CHANNEL in $REPO"
  VERSION="${TAG#v}"

  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  base="https://github.com/$REPO/releases/download/$TAG"
  pkg="aiops-$VERSION.tar.gz"

  info "AIOps $VERSION ($CHANNEL track) from github.com/$REPO"
  curl -fSL --retry 3 --connect-timeout 15 -o "$TMP/$pkg" "$base/$pkg" || die "download of $pkg failed"
  curl -fsSL --retry 3 --connect-timeout 15 -o "$TMP/SHA256SUMS" "$base/SHA256SUMS" || die "download of SHA256SUMS failed"
  (cd "$TMP" && grep " $pkg\$" SHA256SUMS | sha256sum -c --quiet -) || die "checksum mismatch for $pkg"
  info "Checksum verified"

  tar -xzf "$TMP/$pkg" -C "$TMP"
  src="$TMP/aiops-$VERSION"
  if [ -f "$DIR/.env" ]; then
    info "Existing installation in $DIR: upgrading (backup first)"
    "$src/upgrade.sh" --install-dir "$DIR" --channel "$CHANNEL" "${PASS[@]}" </dev/null
  else
    "$src/install.sh" --install-dir "$DIR" --channel "$CHANNEL" --yes "${PASS[@]}" </dev/null
  fi
}

main "$@"
