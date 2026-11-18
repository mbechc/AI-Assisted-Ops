#!/usr/bin/env bash
# AIOps installer. Idempotent: re-running it from a newer package upgrades in place
# (keeps .env, certificates, database, model and users).
set -eEuo pipefail
on_error() {
  local rc=$? line=$1 cmd=$2
  [ -n "${_FAIL_REPORTED:-}" ] && return
  _FAIL_REPORTED=1
  printf "\033[1;31m[FAIL]\033[0m installation failed (exit %s) at line %s: %s\n" "$rc" "$line" "$cmd" >&2
  printf "       Fix the cause and re-run the installer; it is safe to run again.\n" >&2
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
. "$SRC_DIR/scripts/lib.sh"

HOSTNAME_ARG=""
ADMIN_PASSWORD=""
MODEL=""
HTTPS_PORT_ARG=""
HTTP_PORT_ARG=""
WITH_MOCK=0
BUILD_LOCAL=0
CHANNEL=""
OFFLINE_BUNDLE=""
SKIP_DOCKER_INSTALL=0
SKIP_MODEL=0
ASSUME_YES=0

usage() {
  cat <<EOF
Usage: sudo ./install.sh [options]

  --install-dir DIR       Installation directory (default /opt/AIOps)
  --channel TRACK         Update track: production (default) or development
  --hostname NAME         Hostname for the TLS certificate (default: this host's FQDN)
  --admin-password PW     Initial admin password (default: generated and shown once)
  --model TAG             Ollama model (default qwen2.5:7b-instruct)
  --https-port N          HTTPS port (default 443)
  --http-port N           HTTP port, redirects to HTTPS (default 80)
  --with-mock             Also run simulated Catalyst Center 2.3/3.1 for testing
  --build                 Build the images locally instead of pulling them from ghcr.io (development)
  --offline-bundle FILE   Install from an offline bundle (no Internet needed; Docker must be installed)
  --skip-docker-install   Never install Docker (fail if missing)
  --skip-model            Do not pull the LLM model (system runs with rule-based confidence only)
  -y, --yes               Non-interactive
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --install-dir) INSTALL_DIR="$2"; shift 2 ;;
    --hostname) HOSTNAME_ARG="$2"; shift 2 ;;
    --admin-password) ADMIN_PASSWORD="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --https-port) HTTPS_PORT_ARG="$2"; shift 2 ;;
    --http-port) HTTP_PORT_ARG="$2"; shift 2 ;;
    --with-mock) WITH_MOCK=1; shift ;;
    --build) BUILD_LOCAL=1; shift ;;
    --channel)
      case "$2" in production|development) CHANNEL="$2" ;; *) die "--channel must be production or development" ;; esac
      shift 2 ;;
    --offline-bundle) OFFLINE_BUNDLE="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"; shift 2 ;;
    --skip-docker-install) SKIP_DOCKER_INSTALL=1; shift ;;
    --skip-model) SKIP_MODEL=1; shift ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown option $1" ;;
  esac
done
export INSTALL_DIR

VERSION="$(tr -d '[:space:]' <"$SRC_DIR/VERSION")"

# ------------------------------------------------------------------ OS + Docker

detect_os() {
  [ -r /etc/os-release ] || die "cannot detect OS (/etc/os-release missing)"
  # os-release defines VERSION, NAME, ... ; read it in a subshell so it cannot clobber installer variables.
  osr() { (. /etc/os-release; eval "printf '%s' \"\${$1:-}\""); }
  OS_ID="$(osr ID)"; OS_ID="${OS_ID:-unknown}"
  OS_LIKE="$(osr ID_LIKE)"
  OS_VERSION="$(osr VERSION_ID)"
  OS_CODENAME="$(osr VERSION_CODENAME)"
  local PRETTY_NAME; PRETTY_NAME="$(osr PRETTY_NAME)"
  case "$OS_ID" in
    debian|ubuntu) PKG=apt ;;
    rhel|rocky|almalinux|centos|ol|fedora) PKG=dnf ;;
    *)
      case "$OS_LIKE" in
        *debian*) PKG=apt ;;
        *rhel*|*fedora*) PKG=dnf ;;
        *) die "unsupported OS: $OS_ID (supported: Debian, Ubuntu, RHEL and derivatives)" ;;
      esac ;;
  esac
  c_ok "OS: ${PRETTY_NAME:-$OS_ID $OS_VERSION} ($PKG)"
}

install_docker_apt() {
  local repo_os="$OS_ID"
  [ "$repo_os" = "ubuntu" ] || [ "$repo_os" = "debian" ] || repo_os=debian
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${repo_os}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${repo_os} ${OS_CODENAME} stable" \
    >/etc/apt/sources.list.d/docker.list
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

install_docker_dnf() {
  local repo_os=rhel
  case "$OS_ID" in fedora) repo_os=fedora ;; centos) repo_os=centos ;; esac
  dnf -y remove podman-docker runc >/dev/null 2>&1 || true
  dnf -y install dnf-plugins-core curl
  local url="https://download.docker.com/linux/${repo_os}/docker-ce.repo"
  dnf config-manager --add-repo "$url" 2>/dev/null || dnf config-manager addrepo --from-repofile="$url"
  dnf -y install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

ensure_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    c_ok "Docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo present), $(docker compose version --short 2>/dev/null)"
  else
    [ "$SKIP_DOCKER_INSTALL" = 1 ] && die "Docker Engine with the compose plugin is required"
    [ -n "$OFFLINE_BUNDLE" ] && die "offline install requires Docker Engine + compose plugin to be installed first"
    c_info "Installing Docker Engine and compose plugin"
    if [ "$PKG" = apt ]; then install_docker_apt; else install_docker_dnf; fi
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
  docker info >/dev/null 2>&1 || die "Docker daemon is not running"
}

# ------------------------------------------------------------------ preflight

port_in_use() {
  ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$1\$"
}

preflight() {
  local cores mem_gb disk_gb root
  cores="$(nproc)"
  mem_gb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
  root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
  mkdir -p "$root"
  disk_gb=$(( $(df -Pk "$root" | awk 'NR==2 {print $4}') / 1024 / 1024 ))

  # Hard floor: below this the stack cannot run at all. The supported size is in README.md.
  [ "$cores" -ge 4 ] || die "at least 4 CPU cores required (found $cores)"
  [ "$mem_gb" -ge 15 ] || die "at least 16 GB RAM required (found ${mem_gb} GB)"
  [ "$disk_gb" -ge 20 ] || die "at least 20 GB free disk in $root required (found ${disk_gb} GB)"
  [ "$cores" -ge 16 ] && c_ok "CPU cores: $cores" || c_warn "CPU cores: $cores (requirement: 16 vCPU, see README.md)"
  # MemTotal is reported slightly below the installed size.
  [ "$mem_gb" -ge 60 ] && c_ok "Memory: ${mem_gb} GB" || c_warn "Memory: ${mem_gb} GB (requirement: 64 GB, see README.md)"
  [ "$disk_gb" -ge 200 ] && c_ok "Free disk in $root: ${disk_gb} GB" \
    || c_warn "Free disk in $root: ${disk_gb} GB (requirement: 200 GB, see README.md)"

  local running_web=""
  [ -f "$INSTALL_DIR/docker-compose.yml" ] && [ -f "$INSTALL_DIR/.env" ] && running_web="$(compose ps -q web 2>/dev/null || true)"
  for p in "$HTTPS_PORT" "$HTTP_PORT"; do
    if port_in_use "$p" && [ -z "$running_web" ]; then
      die "port $p is already in use (choose another with --https-port/--http-port)"
    fi
  done
  c_ok "Ports: HTTPS $HTTPS_PORT, HTTP $HTTP_PORT"
}

# ------------------------------------------------------------------ files + config

stage_files() {
  mkdir -p "$INSTALL_DIR"
  if [ "$(cd "$INSTALL_DIR" && pwd)" != "$SRC_DIR" ]; then
    c_info "Copying package to $INSTALL_DIR"
    (cd "$SRC_DIR" && tar --exclude=./.env --exclude=./certs --exclude=./data --exclude=./backups \
      --exclude=./dist --exclude=./frontend/node_modules --exclude=./frontend/dist -cf - .) \
      | (cd "$INSTALL_DIR" && tar --no-same-owner -xf -)
  fi
  mkdir -p "$INSTALL_DIR/certs" "$INSTALL_DIR/data/fixtures" "$INSTALL_DIR/data/geoip" "$INSTALL_DIR/data/update/requests" \
    "$INSTALL_DIR/backups"
  find "$INSTALL_DIR" -path "$INSTALL_DIR/data" -prune -o ! -user root -exec chown root:root {} +
  chmod go-w "$INSTALL_DIR"
  chown -R 10001:10001 "$INSTALL_DIR/data"
  chmod 700 "$INSTALL_DIR/backups"
  chmod +x "$INSTALL_DIR"/install.sh "$INSTALL_DIR"/upgrade.sh "$INSTALL_DIR"/scripts/*.sh
}

configure_env() {
  local env="$INSTALL_DIR/.env"
  if [ ! -f "$env" ]; then
    c_info "Generating configuration and secrets"
    cp "$INSTALL_DIR/.env.example" "$env"
    chmod 600 "$env"
    env_set POSTGRES_PASSWORD "$(rand_hex 24)"
    env_set SECRET_KEY "$(rand_hex 32)"
    env_set FERNET_KEY "$(fernet_key)"
    env_set AIOPS_HOSTNAME "${HOSTNAME_ARG:-$(hostname -f 2>/dev/null || hostname)}"
    env_set TZ "$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)"
    env_set COMPOSE_PROJECT_NAME aiops
  fi
  chmod 600 "$env"
  # project_name() falls back to the pre-rename name for an .env without it; make that explicit.
  [ -n "$(env_get COMPOSE_PROJECT_NAME)" ] || env_set COMPOSE_PROJECT_NAME "$(project_name)"
  [ -n "$(env_get AIOPS_UPDATE_REPO)" ] || env_set AIOPS_UPDATE_REPO "$GITHUB_REPO"
  [ -n "$(env_get AIOPS_UPDATE_CHANNEL)" ] || env_set AIOPS_UPDATE_CHANNEL production
  [ -n "$CHANNEL" ] && env_set AIOPS_UPDATE_CHANNEL "$CHANNEL"
  [ -n "$(env_get AIOPS_IMAGE_PREFIX)" ] || env_set AIOPS_IMAGE_PREFIX "ghcr.io/$(echo "${GITHUB_REPO%%/*}" | tr '[:upper:]' '[:lower:]')/aiops"
  env_set AIOPS_VERSION "$VERSION"
  [ -n "$HOSTNAME_ARG" ] && env_set AIOPS_HOSTNAME "$HOSTNAME_ARG"
  [ -n "$MODEL" ] && env_set OLLAMA_MODEL "$MODEL"
  [ -n "$HTTPS_PORT_ARG" ] && env_set HTTPS_PORT "$HTTPS_PORT_ARG"
  [ -n "$HTTP_PORT_ARG" ] && env_set HTTP_PORT "$HTTP_PORT_ARG"
  [ "$WITH_MOCK" = 1 ] && env_set COMPOSE_PROFILES mock
  HTTPS_PORT="$(env_get HTTPS_PORT)"
  HTTP_PORT="$(env_get HTTP_PORT)"
  MODEL="$(env_get OLLAMA_MODEL)"
  return 0
}

open_firewall() {
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="${HTTPS_PORT}/tcp" --add-port="${HTTP_PORT}/tcp" >/dev/null
    firewall-cmd --reload >/dev/null
    c_ok "firewalld: opened ${HTTPS_PORT}/tcp and ${HTTP_PORT}/tcp"
  elif command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "${HTTPS_PORT}/tcp" >/dev/null
    ufw allow "${HTTP_PORT}/tcp" >/dev/null
    c_ok "ufw: opened ${HTTPS_PORT}/tcp and ${HTTP_PORT}/tcp"
  fi
}

# ------------------------------------------------------------------ host updater

# The portal drops request files into data/update/requests; this path unit runs scripts/update.sh
# on the host. Containers never get the Docker socket or root for updates.
install_updater() {
  if [ ! -d /run/systemd/system ]; then
    c_warn "systemd not found: in-portal updates and the Dashboard internet status are unavailable"
    return
  fi
  mkdir -p "$INSTALL_DIR/data/update/requests"
  chown -R 10001:10001 "$INSTALL_DIR/data/update"
  cat >/etc/systemd/system/aiops-updater.service <<EOF
[Unit]
Description=AIOps updater (GitHub reachability, release lookup, updates)
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$INSTALL_DIR/scripts/update.sh --process
TimeoutStartSec=3h
EOF
  cat >/etc/systemd/system/aiops-updater.path <<EOF
[Unit]
Description=AIOps updater: requests from the portal

[Path]
DirectoryNotEmpty=$INSTALL_DIR/data/update/requests

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable aiops-updater.path >/dev/null 2>&1
  systemctl restart aiops-updater.path
  touch "$INSTALL_DIR/data/update/requests/$(date +%s%N)-check"
  c_ok "Host updater: aiops-updater.path (systemd)"
}

# ------------------------------------------------------------------ images + model

load_offline_bundle() {
  local tmp
  tmp="$(mktemp -d)"
  c_info "Loading offline bundle $OFFLINE_BUNDLE"
  tar -xf "$OFFLINE_BUNDLE" -C "$tmp"
  docker load -i "$tmp/images.tar"
  if [ -f "$tmp/ollama-models.tgz" ]; then
    docker volume create "$(project_name)_ollama" >/dev/null
    docker run --rm -v "$(project_name)_ollama":/root/.ollama -v "$tmp":/in --entrypoint tar ollama/ollama:0.40.16 \
      xzf /in/ollama-models.tgz -C /root/.ollama
    c_ok "LLM model restored from bundle"
  fi
  rm -rf "$tmp"
}

build_images() {
  c_info "Pulling base images and building AIOps $VERSION locally"
  # Pull every image that is not built locally (compose < 2.22 lacks --ignore-buildable).
  compose pull --ignore-buildable 2>/dev/null || compose pull postgres redis ollama logproxy
  compose build --pull
}

# Prebuilt images from ghcr.io; on upgrades only changed layers are downloaded.
pull_images() {
  c_info "Pulling AIOps $VERSION images ($(env_get AIOPS_IMAGE_PREFIX)-backend, -web) and base images"
  compose pull --quiet || die "image pull failed: check Internet/proxy access to ghcr.io (or use --build)"
}

pull_model() {
  if [ "$SKIP_MODEL" = 1 ]; then c_warn "Skipping model pull"; return; fi
  if compose exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$MODEL"; then
    c_ok "LLM model $MODEL already present"
    return
  fi
  [ -n "$OFFLINE_BUNDLE" ] && { c_warn "Model $MODEL not in bundle; LLM review disabled until it is added"; return; }
  c_info "Pulling LLM model $MODEL (several GB, one time)"
  compose exec -T ollama ollama pull "$MODEL"
}

# ------------------------------------------------------------------ main

main() {
  require_root
  c_info "AIOps $VERSION installer -> $INSTALL_DIR"
  detect_os
  ensure_docker
  HTTPS_PORT="${HTTPS_PORT_ARG:-443}"
  HTTP_PORT="${HTTP_PORT_ARG:-80}"
  if [ -f "$INSTALL_DIR/.env" ]; then HTTPS_PORT="$(env_get HTTPS_PORT)"; HTTP_PORT="$(env_get HTTP_PORT)"; fi
  [ -n "$HTTPS_PORT_ARG" ] && HTTPS_PORT="$HTTPS_PORT_ARG"
  [ -n "$HTTP_PORT_ARG" ] && HTTP_PORT="$HTTP_PORT_ARG"
  preflight

  if [ "$ASSUME_YES" != 1 ] && [ -t 0 ]; then
    read -r -p "Install AIOps $VERSION into $INSTALL_DIR? [Y/n] " ans
    case "${ans:-y}" in y|Y|yes) ;; *) die "aborted" ;; esac
  fi

  stage_files
  configure_env
  open_firewall
  install_updater

  if [ -n "$OFFLINE_BUNDLE" ]; then load_offline_bundle
  elif [ "$BUILD_LOCAL" = 1 ]; then build_images
  else pull_images; fi

  c_info "TLS certificate"
  local ips
  ips="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9.]+$' | sed 's/^/--ip /' | tr '\n' ' ')"
  # shellcheck disable=SC2086
  compose run --rm --no-deps --user 0 -v "$INSTALL_DIR/certs:/certs:z" api manage gen-cert \
    --hostname "$(env_get AIOPS_HOSTNAME)" $ips --out /certs

  c_info "Starting database, queue and local LLM"
  compose up -d postgres redis ollama
  wait_healthy postgres 180 || die "postgres did not become healthy"
  wait_healthy redis 120 || die "redis did not become healthy"
  wait_healthy ollama 180 || die "ollama did not become healthy"
  pull_model

  c_info "Database migrations"
  compose run --rm api migrate
  compose run --rm api manage bootstrap

  local admin_out=""
  if [ -n "$ADMIN_PASSWORD" ]; then
    compose run --rm api manage create-admin --username admin --password "$ADMIN_PASSWORD" --reset >/dev/null
    admin_out="$ADMIN_PASSWORD"
  else
    admin_out="$(compose run --rm -T api manage create-admin --username admin --print-password | tail -1)"
    case "$admin_out" in *"already exists"*) admin_out="" ;; esac
  fi

  c_info "Starting all services"
  compose up -d --remove-orphans
  for svc in $(services); do
    wait_healthy "$svc" 300 || { compose logs --tail 50 "$svc"; die "$svc did not become healthy"; }
  done
  if [ "$(env_get COMPOSE_PROFILES)" = "mock" ]; then
    compose run --rm api manage add-mock-sources
  fi

  local hc_args=""
  if ! compose exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$MODEL"; then
    hc_args="--allow-no-model"
  fi
  # shellcheck disable=SC2086
  "$INSTALL_DIR/scripts/healthcheck.sh" $hc_args || die "post-install self-test failed"

  echo
  c_ok "AIOps $VERSION is installed and running."
  echo "    URL:   https://$(env_get AIOPS_HOSTNAME):${HTTPS_PORT}/"
  if [ -n "$admin_out" ]; then
    umask 077
    printf 'admin\n%s\n' "$admin_out" >"$INSTALL_DIR/initial-admin-password.txt"
    echo "    Login: admin / $admin_out"
    echo "           (also saved in $INSTALL_DIR/initial-admin-password.txt; change it after first login)"
  else
    echo "    Login: existing users unchanged"
  fi
  echo "    Updates: Settings > Updates (track: $(env_get AIOPS_UPDATE_CHANNEL)). Internet access is only needed for updates."
}

main "$@"
