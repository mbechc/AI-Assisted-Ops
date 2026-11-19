# shellcheck shell=bash
# Shared helpers for AIOps scripts. Source, do not execute.

INSTALL_DIR="${INSTALL_DIR:-/opt/AIOps}"
GITHUB_REPO="${GITHUB_REPO:-mbechc/AI-Assisted-Ops}"

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

# Installs from before the rename keep their project name (and with it their volumes) via .env.
project_name() {
  local p
  p="$(env_get COMPOSE_PROJECT_NAME)"
  if [ -z "$p" ] && [ -f "$INSTALL_DIR/.env" ]; then p=ciscoaiops; fi
  echo "${p:-aiops}"
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