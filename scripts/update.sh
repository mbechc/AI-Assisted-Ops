#!/usr/bin/env bash
# Host-side updater. Runs as root on the Linux host, never in a container, so every test uses the
# host's own DNS, proxy and firewall path (the same path docker pull and the download use).
#
#   --process               handle requests queued by the portal (run by steward-updater.service)
#   --check                 GitHub reachability test plus release lookup
#   --connectivity          reachability test only (Dashboard: Connected / AirGapped)
#   --channel TRACK         switch the update track: production or development
#   --version YYYY.M.R      install that release: download, verify checksum, run its upgrade.sh
#   --llm ACTION            Local LLM: enable (start ollama, pull the model), disable (stop, keep model)
#                           or remove (stop and delete the model volume)
#   --endpoints-override H  test only: probe host H instead of the GitHub endpoints
#
# The portal never runs this; it only drops request files into data/update/requests, named
# <epoch-ns>-check, -connectivity, -channel-<track>, -install-<version> or -llm-<action>. Results go to
# data/update/{status,install,connectivity,releases-meta,llm}.json, releases.json, update.log and llm.log.
set -euo pipefail

if [ "${STEWARD_UPDATER_COPY:-}" != 1 ]; then
  # An install replaces this file while it runs and bash reads scripts lazily: run from a copy.
  INSTALL_DIR="${INSTALL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
  copy="$(mktemp /tmp/steward-update.XXXXXX)"
  cp "${BASH_SOURCE[0]}" "$copy"
  STEWARD_UPDATER_COPY=1 STEWARD_UPDATER_SELF="$copy" INSTALL_DIR="$INSTALL_DIR" exec bash "$copy" "$@"
fi
trap 'rm -f "$STEWARD_UPDATER_SELF"' EXIT
# shellcheck source=scripts/lib.sh
. "$INSTALL_DIR/scripts/lib.sh"
require_root
[ -f "$INSTALL_DIR/.env" ] || die "no installation found in $INSTALL_DIR"

UPDATER_VERSION=2
UPD="$INSTALL_DIR/data/update"
REQ="$UPD/requests"
REPO="$(env_get STEWARD_UPDATE_REPO)"; REPO="${REPO:-$GITHUB_REPO}"
OVERRIDE=()

for v in HTTPS_PROXY https_proxy HTTP_PROXY http_proxy NO_PROXY no_proxy; do
  val="$(env_get "$v")"
  [ -n "$val" ] && export "$v=$val"
done

mkdir -p "$REQ"
chown 10001:10001 "$UPD" "$REQ"

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*"; }
now_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

jstr() {
  local s
  s="$(printf '%s' "$1" | tr -d '\000-\037')"
  s="${s//\\/\\\\}"
  printf '"%s"' "${s//\"/\\\"}"
}
jopt() { if [ -n "$1" ]; then jstr "$1"; else printf null; fi; }

write_file() {
  local tmp
  tmp="$(mktemp "$UPD/.tmp.XXXXXX")"
  cat >"$tmp"
  chmod 644 "$tmp"
  mv -f "$tmp" "$1"
}

valid_version() { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]]; }

# Order: 2026.11.0 > 2026.11.0-dev.3 > 2026.11.0-dev.2 > 0.2.5 (calendar versions; 0.x.y came before).
version_gt() {
  [ "$1" = "$2" ] && return 1
  local am="${1%%-*}" bm="${2%%-*}" ap="" bp=""
  [[ "$1" == *-* ]] && ap="${1#*-}"
  [[ "$2" == *-* ]] && bp="${2#*-}"
  if [ "$am" != "$bm" ]; then
    [ "$(printf '%s\n%s\n' "$am" "$bm" | sort -V | tail -1)" = "$am" ]
    return
  fi
  [ -z "$ap" ] && return 0
  [ -z "$bp" ] && return 1
  [ "$(printf '%s\n%s\n' "$ap" "$bp" | sort -V | tail -1)" = "$ap" ]
}

write_status() {
  local root free total disk=null
  root="$(docker_root)"
  free="$(df_gb "$root" 2>/dev/null || true)"
  total="$(df_gb "$root" total 2>/dev/null || true)"
  if [ -n "$free" ] && [ -n "$total" ]; then
    disk="{\"path\": $(jstr "$root"), \"free_gb\": $free, \"total_gb\": $total}"
  fi
  write_file "$UPD/status.json" <<EOF
{"updated_at": "$(now_iso)", "current_version": $(jstr "$(env_get STEWARD_VERSION)"), "channel": $(jstr "$(env_get STEWARD_UPDATE_CHANNEL)"), "repo": $(jstr "$REPO"), "updater_version": $UPDATER_VERSION, "disk": $disk}
EOF
}

INSTALL_STARTED=""
install_state() {  # state version error
  local finished=null
  [ "$1" = running ] && INSTALL_STARTED="$(now_iso)"
  [ "$1" != running ] && finished="\"$(now_iso)\""
  write_file "$UPD/install.json" <<EOF
{"state": "$1", "version": $(jopt "$2"), "started_at": $(jopt "$INSTALL_STARTED"), "finished_at": $finished, "error": $(jopt "${3:-}")}
EOF
}

curl_error() {
  case "$1" in
    5) echo "proxy name not resolved" ;;
    6) echo "DNS lookup failed" ;;
    7) echo "connection refused or blocked" ;;
    28) echo "timeout" ;;
    35|53|54|58|59|60|66|77|80|83|90|91) echo "TLS error (curl $1)" ;;
    47) echo "too many redirects" ;;
    52|56) echo "connection reset (proxy or firewall?)" ;;
    97) echo "proxy handshake failed" ;;
    *) echo "curl error $1" ;;
  esac
}

# probe LABEL URL MODE -> one JSON object. MODE: ok2xx (2xx/3xx required), any (any HTTP answer),
# follow (follow redirects, label becomes the final host).
probe() {
  local label="$1" url="$2" mode="$3" out rc=0 code t final ms ok=false err=""
  local args=(-sS -o /dev/null --connect-timeout 8 --max-time 20 -w '%{http_code} %{time_total} %{url_effective}')
  [ "$mode" = follow ] && args+=(-L)
  out="$(curl "${args[@]}" "$url" 2>/dev/null)" || rc=$?
  read -r code t final <<<"$out"
  ms="$(awk -v t="${t:-0}" 'BEGIN { printf "%d", t * 1000 }')"
  if [ "$mode" = follow ] && [ -n "${final:-}" ]; then
    label="$(printf '%s' "$final" | sed -E 's#^https?://([^/:]+).*#\1#')"
  fi
  if [ "$rc" -ne 0 ]; then err="$(curl_error "$rc")"
  elif [ "$mode" = any ] || [[ "$code" =~ ^[23] ]]; then ok=true
  else err="HTTP $code"; fi
  printf '{"host": %s, "ok": %s, "ms": %s, "error": %s}' "$(jstr "$label")" "$ok" "$ms" "$(jopt "$err")"
}

CONNECTED=0
connectivity() {
  local eps=() e prefix image dl
  if [ ${#OVERRIDE[@]} -gt 0 ]; then
    for e in "${OVERRIDE[@]}"; do eps+=("$(probe "$e" "https://$e/" any)"); done
  else
    eps+=("$(probe api.github.com https://api.github.com/rate_limit ok2xx)")
    eps+=("$(probe github.com "https://github.com/$REPO" ok2xx)")
    # Release downloads redirect to a *.githubusercontent.com host; test the real path when a release exists.
    dl="$(probe downloads "https://github.com/$REPO/releases/latest/download/SHA256SUMS" follow)"
    case "$dl" in *'"error": "HTTP 404"'*) dl="$(probe objects.githubusercontent.com https://objects.githubusercontent.com/ any)" ;; esac
    eps+=("$dl")
    prefix="$(env_get STEWARD_IMAGE_PREFIX)"; prefix="${prefix:-ghcr.io/mbechc/steward}"
    image="${prefix#*/}-backend"
    e="$(probe "${prefix%%/*}" "https://${prefix%%/*}/token?scope=repository:${image}:pull&service=${prefix%%/*}" ok2xx)"
    case "$e" in
      *'"error": "HTTP 401"'*|*'"error": "HTTP 403"'*)
        e="$(sed -E "s#\"error\": \"HTTP (40[13])\"#\"error\": \"HTTP \\1: image $image is not published or not public\"#" <<<"$e")" ;;
    esac
    eps+=("$e")
    # ghcr.io serves image layers from here.
    [ "${prefix%%/*}" = ghcr.io ] && eps+=("$(probe pkg-containers.githubusercontent.com https://pkg-containers.githubusercontent.com/ any)")
  fi
  local status=connected joined
  for e in "${eps[@]}"; do case "$e" in *'"ok": false'*) status=airgapped ;; esac; done
  joined="$(IFS=,; echo "${eps[*]}")"
  write_file "$UPD/connectivity.json" <<EOF
{"checked_at": "$(now_iso)", "status": "$status", "override": $([ ${#OVERRIDE[@]} -gt 0 ] && echo true || echo false), "endpoints": [$joined]}
EOF
  log "connectivity: $status"
  [ "$status" = connected ] && CONNECTED=1 || CONNECTED=0
}

releases() {
  local tmp rc=0
  tmp="$(mktemp "$UPD/.tmp.XXXXXX")"
  curl -fsS --connect-timeout 8 --max-time 30 -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$REPO/releases?per_page=30" -o "$tmp" 2>/dev/null || rc=$?
  if [ "$rc" -eq 0 ]; then
    chmod 644 "$tmp"
    mv -f "$tmp" "$UPD/releases.json"
    printf '{"checked_at": "%s", "ok": true, "error": null}\n' "$(now_iso)" | write_file "$UPD/releases-meta.json"
    log "release list refreshed"
  else
    rm -f "$tmp"
    printf '{"checked_at": "%s", "ok": false, "error": %s}\n' "$(now_iso)" "$(jstr "release lookup failed: $(curl_error "$rc")")" \
      | write_file "$UPD/releases-meta.json"
    log "release lookup failed ($rc)"
  fi
}

check() {
  connectivity
  releases
}

set_channel() {
  case "$1" in production|development) ;; *) die "track must be production or development" ;; esac
  env_set STEWARD_UPDATE_CHANNEL "$1"
  write_status
  log "track set to $1"
}

install_version() {
  local v="$1" cur work base pkg rc=0
  valid_version "$v" || { install_state failed "$v" "invalid version"; return 1; }
  cur="$(env_get STEWARD_VERSION)"
  if ! version_gt "$v" "$cur"; then
    install_state failed "$v" "$v is not newer than the installed $cur"
    return 1
  fi
  install_state running "$v"
  : >"$UPD/update.log"
  chmod 644 "$UPD/update.log"
  local root free msg
  root="$(docker_root)"
  prune_images "$cur" >>"$UPD/update.log" 2>&1
  free="$(df_gb "$root")"
  if [ "$free" -lt "$UPGRADE_MIN_FREE_GB" ]; then
    msg="at least $UPGRADE_MIN_FREE_GB GB free disk in $root required for an upgrade (found $free GB). $(disk_hint)"
    echo "$msg" >>"$UPD/update.log"
    install_state failed "$v" "$msg"
    write_status
    return 1
  fi
  work="$(mktemp -d)"
  pkg="steward-$v.tar.gz"
  base="https://github.com/$REPO/releases/download/v$v"
  (
    set -e
    echo "Updating Steward $cur -> $v ($(now_iso))"
    echo "==> Downloading $pkg"
    curl -fSL --retry 3 --connect-timeout 15 -o "$work/$pkg" "$base/$pkg"
    curl -fsSL --retry 3 --connect-timeout 15 -o "$work/SHA256SUMS" "$base/SHA256SUMS"
    (cd "$work" && grep " $pkg\$" SHA256SUMS | sha256sum -c -)
    tar -xzf "$work/$pkg" -C "$work"
    echo "==> Running upgrade.sh (backup, pull changed image layers, migrate, restart)"
    "$work/steward-$v/upgrade.sh" --install-dir "$INSTALL_DIR"
  ) >>"$UPD/update.log" 2>&1 || rc=$?
  rm -rf "$work"
  if [ "$rc" -eq 0 ]; then
    echo "Update to $v finished ($(now_iso))" >>"$UPD/update.log"
    install_state succeeded "$v"
    log "installed $v"
  else
    local last
    last="$(sed -e 's/\x1b\[[0-9;]*m//g' "$UPD/update.log" | grep -v '^\s*$' | tail -1)"
    install_state failed "$v" "exit $rc: ${last:-see update.log}"
    log "install of $v failed ($rc)"
  fi
  write_status
  return "$rc"
}

recover_interrupted() {
  if grep -q '"state": "running"' "$UPD/install.json" 2>/dev/null; then
    local v
    v="$(sed -E 's/.*"version": "([^"]*)".*/\1/' "$UPD/install.json")"
    install_state failed "$v" "interrupted (host restart or updater stopped); restore the pre-update backup if needed"
  fi
  if grep -qE '"state": "(installing|pulling)"' "$UPD/llm.json" 2>/dev/null; then
    llm_state failed "interrupted (host restart or updater stopped); enable Local LLM again to retry"
  fi
}

# ------------------------------------------------------------------ local LLM (Settings > AI assistance)

llm_state() {  # state [error]
  local model; model="$(env_get OLLAMA_MODEL)"
  write_file "$UPD/llm.json" <<EOF
{"state": "$1", "model": $(jstr "${model:-qwen2.5:7b-instruct}"), "updated_at": "$(now_iso)", "error": $(jopt "${2:-}")}
EOF
}

llm_containers() {
  docker ps -aq --filter "label=com.docker.compose.project=$(project_name)" --filter "label=com.docker.compose.service=ollama"
}

llm_enable() {
  local rc=0
  : >"$UPD/llm.log"
  chmod 644 "$UPD/llm.log"
  llm_state installing
  (
    set -e
    echo "Enabling Local LLM ($(now_iso))"
    profile_add llm
    echo "==> Starting the ollama container (image pulled on first use)"
    compose up -d ollama
    wait_healthy ollama 300 || { echo "ollama did not become healthy"; exit 1; }
    llm_state pulling
    echo "==> Model"
    llm_pull_model
  ) >>"$UPD/llm.log" 2>&1 || rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "Local LLM ready ($(now_iso))" >>"$UPD/llm.log"
    llm_state ready
    log "local LLM enabled"
  else
    local last
    last="$(sed -e 's/\x1b\[[0-9;]*m//g' "$UPD/llm.log" | grep -v '^\s*$' | tail -1)"
    llm_state failed "exit $rc: ${last:-see llm.log}"
    log "local LLM enable failed ($rc)"
  fi
  return "$rc"
}

llm_disable() {
  local ids
  profile_remove llm
  ids="$(llm_containers)"
  # shellcheck disable=SC2086
  [ -n "$ids" ] && docker rm -f $ids >/dev/null
  if [ "${1:-}" = remove ]; then
    docker volume rm -f "$(project_name)_ollama" >/dev/null 2>&1 || true
    rm -f "$INSTALL_DIR/data/llm/ollama-models.tgz"
    ids="$(docker image ls -q ollama/ollama 2>/dev/null | sort -u)"
    # shellcheck disable=SC2086
    [ -n "$ids" ] && { docker image rm -f $ids >/dev/null 2>&1 || true; }
    llm_state removed
    log "local LLM removed (container, model and image)"
  else
    llm_state disabled
    log "local LLM disabled (model kept)"
  fi
}

process() {
  recover_interrupted
  local n=0 f name files
  shopt -s nullglob
  while [ $n -lt 100 ]; do
    files=("$REQ"/*)
    [ ${#files[@]} -gt 0 ] || break
    f="${files[0]}"; name="$(basename "$f")"
    rm -f "$f"
    n=$((n + 1))
    if [[ "$name" =~ ^[0-9]+-check$ ]]; then check || true
    elif [[ "$name" =~ ^[0-9]+-connectivity$ ]]; then connectivity || true
    elif [[ "$name" =~ ^[0-9]+-channel-(production|development)$ ]]; then set_channel "${BASH_REMATCH[1]}" && releases || true
    elif [[ "$name" =~ ^[0-9]+-install-([0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?)$ ]]; then
      install_version "${BASH_REMATCH[1]}" && check || true
    elif [[ "$name" =~ ^[0-9]+-llm-enable$ ]]; then llm_enable || true
    elif [[ "$name" =~ ^[0-9]+-llm-disable$ ]]; then llm_disable || true
    elif [[ "$name" =~ ^[0-9]+-llm-remove$ ]]; then llm_disable remove || true
    else
      log "ignored unknown request $name"
    fi
  done
  write_status
}

ACTION=""
ARG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --process|--check|--connectivity) ACTION="${1#--}"; shift ;;
    --channel) ACTION=channel; ARG="${2:-}"; shift 2 ;;
    --version) ACTION=install; ARG="${2#v}"; shift 2 ;;
    --llm)
      case "${2:-}" in enable|disable|remove) ;; *) die "--llm must be enable, disable or remove" ;; esac
      ACTION=llm; ARG="$2"; shift 2 ;;
    --endpoints-override) OVERRIDE+=("$2"); shift 2 ;;
    -h|--help) sed -n '2,15p' "$STEWARD_UPDATER_SELF" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option $1" ;;
  esac
done
[ -n "$ACTION" ] || die "nothing to do (see --help)"

exec 9>"$UPD/.lock"
flock -w 7200 9 || die "another update action is still running"

case "$ACTION" in
  process) process ;;
  check) check; write_status ;;
  connectivity) connectivity; write_status ;;
  channel) set_channel "$ARG" ;;
  install) recover_interrupted; install_version "$ARG" && check ;;
  llm)
    case "$ARG" in enable) llm_enable ;; disable) llm_disable ;; remove) llm_disable remove ;; esac ;;
esac
