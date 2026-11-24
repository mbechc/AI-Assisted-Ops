# shellcheck shell=bash
# Shared helpers for Steward scripts. Source, do not execute.

INSTALL_DIR="${INSTALL_DIR:-/opt/Steward}"
GITHUB_REPO="${GITHUB_REPO:-mbechc/Steward}"

c_info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
c_ok()   { printf '\033[1;32m[ OK ]\033[0m %s\n' "$*"; }
c_warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
c_err()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; }
die()    { c_err "$*"; exit 1; }

require_root() {
  [ "$(id -u)" -eq 0 ] || die "run as root (sudo $0 ...)"
}

env_get() {
  { grep -E "^$1=" "$INSTALL_DIR/.env" 2>/dev/null || true; } | tail -1 | cut -d= -f2-
}

project_name() {
  local p
  p="$(env_get COMPOSE_PROJECT_NAME)"
  echo "${p:-steward}"
}

compose() {
  docker compose -p "$(project_name)" --project-directory "$INSTALL_DIR" -f "$INSTALL_DIR/docker-compose.yml" \
    --env-file "$INSTALL_DIR/.env" "$@"
}

env_set() {
  local key="$1" value="$2" file="$INSTALL_DIR/.env"
  if grep -qE "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >>"$file"
  fi
}

env_unset() {
  sed -i "/^$1=/d" "$INSTALL_DIR/.env"
}

# COMPOSE_PROFILES in .env is a comma list (mock, llm); compose reads it for every command.
profile_has() { case ",$(env_get COMPOSE_PROFILES)," in *",$1,"*) return 0 ;; *) return 1 ;; esac; }
profile_add() {
  profile_has "$1" && return 0
  local cur; cur="$(env_get COMPOSE_PROFILES)"
  env_set COMPOSE_PROFILES "${cur:+$cur,}$1"
}
profile_remove() {
  local out
  out="$(env_get COMPOSE_PROFILES | tr ',' '\n' | { grep -vx -e "$1" -e '' || true; } | paste -sd, -)"
  if [ -n "$out" ]; then env_set COMPOSE_PROFILES "$out"; else env_unset COMPOSE_PROFILES; fi
}

# Pulls OLLAMA_MODEL into the running ollama container unless present. Offline: imports
# data/llm/ollama-models.tgz when it exists.
llm_pull_model() {
  local model; model="$(env_get OLLAMA_MODEL)"; model="${model:-qwen2.5:7b-instruct}"
  if compose exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$model"; then
    echo "LLM model $model already present"
    return 0
  fi
  if [ -f "$INSTALL_DIR/data/llm/ollama-models.tgz" ]; then
    echo "Importing LLM model from data/llm/ollama-models.tgz"
    compose exec -T ollama tar xzf - -C /root/.ollama <"$INSTALL_DIR/data/llm/ollama-models.tgz"
    compose restart ollama >/dev/null
    wait_healthy ollama 180 || return 1
    compose exec -T ollama ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$model" && return 0
    echo "model $model is not in the imported archive"
    return 1
  fi
  echo "Pulling LLM model $model (several GB, one time)"
  compose exec -T ollama ollama pull "$model"
}

# ------------------------------------------------------------------ disk space

FRESH_MIN_FREE_GB=20
UPGRADE_MIN_FREE_GB=5

docker_root() { docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker; }

# df_gb PATH [free|total]: whole GB.
df_gb() {
  local col=4
  [ "${2:-free}" = total ] && col=2
  echo $(( $(df -Pk "$1" | awk -v c="$col" 'NR==2 {print $c}') / 1024 / 1024 ))
}

# One line naming the largest users of the Docker disk, for failure messages.
disk_hint() {
  local df images volumes backups
  df="$(docker system df --format '{{.Type}}|{{.Size}}' 2>/dev/null || true)"
  images="$(awk -F'|' '$1 == "Images" {print $2}' <<<"$df")"
  volumes="$(awk -F'|' '$1 == "Local Volumes" {print $2}' <<<"$df")"
  backups="$(du -sh "$INSTALL_DIR/backups" 2>/dev/null | cut -f1)"
  echo "Largest: images ${images:-?}, volumes ${volumes:-?}, backups ${backups:-?}. Free space with: docker image prune -a"
}

# prune_images KEEP...: remove Steward images of every other version, then dangling layers.
# Other images on the host and all volumes are left alone; never fails.
prune_images() {
  local prefix ref keep removed=0 any=""
  for keep in "$@"; do [ -n "$keep" ] && any=1; done
  [ -n "$any" ] || return 0
  prefix="$(env_get STEWARD_IMAGE_PREFIX)"
  prefix="${prefix:-ghcr.io/mbechc/steward}"
  while read -r ref; do
    for keep in "$@"; do [ "${ref##*:}" = "$keep" ] && continue 2; done
    if docker image rm "$ref" >/dev/null 2>&1; then
      removed=$((removed + 1))
    else
      c_warn "could not remove image $ref (still in use?)"
    fi
  done < <(docker image ls --format '{{.Repository}}:{{.Tag}}' 2>/dev/null |
    awk -v p="$prefix" '(index($0, p "-backend:") == 1 || index($0, p "-web:") == 1) && $0 !~ /:<none>$/')
  docker image prune -f >/dev/null 2>&1 || true
  if [ "$removed" -gt 0 ]; then c_ok "Removed $removed image(s) of earlier Steward versions"; fi
  return 0
}

rand_hex() { head -c "${1:-32}" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
fernet_key() { head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n'; }

# Services of the active profiles, as defined in docker-compose.yml (new workers are picked up automatically).
services() { compose config --services; }

# "healthy" for containers with a healthcheck, or running ones without one; otherwise the raw state.
service_state() {
  local s
  s="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null)"
  [ "$s" = "running" ] && s="healthy"
  echo "${s:-missing}"
}

wait_healthy() {
  local svc="$1" timeout="${2:-300}" waited=0 cid state
  while [ "$waited" -lt "$timeout" ]; do
    cid="$(compose ps -q "$svc" 2>/dev/null)"
    if [ -n "$cid" ]; then
      state="$(service_state "$cid")"
      [ "$state" = "healthy" ] && return 0
    fi
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

# ------------------------------------------------------------------ terms of use (DISCLAIMER.md)
# Acceptance is recorded once per installation in data/terms/acceptance.json (read-only in the containers).

TERMS_RECORD_REL="data/terms/acceptance.json"

terms_accepted() { [ -s "$INSTALL_DIR/$TERMS_RECORD_REL" ]; }

have_tty() { { : </dev/tty; } 2>/dev/null; }

# terms_record FILE METHOD: write the acceptance record (method: typed | flag).
terms_record() {
  local terms="$1" method="$2" out="$INSTALL_DIR/$TERMS_RECORD_REL" ver sha
  ver="$(sed -n 's/^Terms version \([0-9][0-9]*\).*/\1/p' "$terms" | head -1)"
  sha="$(sha256sum "$terms" | awk '{print $1}')"
  mkdir -p "$(dirname "$out")"
  printf '{"terms_version": %s, "sha256": "%s", "accepted_at": "%s", "accepted_by": "%s", "host": "%s", "steward_version": "%s", "method": "%s"}\n' \
    "${ver:-0}" "$sha" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${SUDO_USER:-$(id -un)}" "$(hostname)" \
    "${VERSION:-unknown}" "$method" >"$out"
  chown -R 10001:10001 "$(dirname "$out")" 2>/dev/null || true
  chmod 644 "$out"
}

# terms_prompt FILE: show the terms on the terminal and return 0 only if the user types ACCEPT.
terms_prompt() {
  local ans
  { printf '\n'; cat "$1"; printf '\n'; } >/dev/tty
  read -r -p "Type ACCEPT to accept these terms, anything else aborts: " ans </dev/tty 2>/dev/tty || return 1
  [ "$ans" = "ACCEPT" ]
}
