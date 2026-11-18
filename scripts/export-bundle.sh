#!/usr/bin/env bash
# Create a fully offline bundle from an installed (or built) system: package + all images + LLM model.
# Install elsewhere with:  tar xf <bundle>; cd aiops-<ver>; sudo ./install.sh --offline-bundle ../<bundle>
set -euo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
require_root

version="$(tr -d '[:space:]' <"$INSTALL_DIR/VERSION")"
out="${1:-$INSTALL_DIR/dist/aiops-offline-$version.tar}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

c_info "Saving images"
images="$(COMPOSE_PROFILES=mock compose config --images | sort -u)"
# shellcheck disable=SC2086
docker save -o "$work/images.tar" $images

c_info "Saving LLM model volume"
docker run --rm -v "$(project_name)_ollama":/root/.ollama -v "$work":/out --entrypoint tar ollama/ollama:0.40.16 \
  czf /out/ollama-models.tgz -C /root/.ollama .

c_info "Adding installation package"
mkdir -p "$work/aiops-$version"
(cd "$INSTALL_DIR" && tar --exclude=./.env --exclude=./certs --exclude=./data --exclude=./backups --exclude=./dist \
  --exclude=./initial-admin-password.txt -cf - .) | (cd "$work/aiops-$version" && tar -xf -)

mkdir -p "$(dirname "$out")"
tar -cf "$out" -C "$work" images.tar ollama-models.tgz "aiops-$version"
c_ok "Offline bundle: $out ($(du -h "$out" | cut -f1))"
