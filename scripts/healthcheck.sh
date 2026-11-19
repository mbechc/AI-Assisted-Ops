#!/usr/bin/env bash
# Post-install / support self-test. Exit code 0 when the system is fully operational.
# The AI check follows Settings > AI assistance: nothing for No AI, the ollama model for Local LLM,
# a probe request for Azure OpenAI.
#   --allow-no-model   do not fail while the Local LLM is still being installed (or failed to install)
set -uo pipefail
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

SELFTEST_ARGS="--llm-probe"
[ "${1:-}" = "--allow-no-model" ] && SELFTEST_ARGS="--allow-no-model"

fail=0
port="$(env_get HTTPS_PORT)"
http_ok() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsk -o /dev/null "https://127.0.0.1:${port}$1"
  else
    compose exec -T web wget -q -O /dev/null --no-check-certificate "https://127.0.0.1$1"
  fi
}

for svc in $(services); do
  cid="$(compose ps -q "$svc" 2>/dev/null)"
  state="missing"
  [ -n "$cid" ] && state="$(service_state "$cid")"
  if [ "$state" = "healthy" ]; then c_ok "container $svc: healthy"; else c_err "container $svc: $state"; fail=1; fi
done

if http_ok "/healthz"; then c_ok "portal reachable over TLS on port ${port}"; else c_err "portal not reachable on port ${port}"; fail=1; fi
if http_ok "/api/health"; then c_ok "API reachable through the proxy"; else c_err "API not reachable through the proxy"; fail=1; fi

# shellcheck disable=SC2086
compose exec -T api python -m app.manage selftest $SELFTEST_ARGS || fail=1

exit "$fail"
