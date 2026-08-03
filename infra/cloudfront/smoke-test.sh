#!/usr/bin/env bash
# Smoke-test the production distribution's behaviors (works pre- and
# post-alias: pass a hostname argument to override, default = the
# *.cloudfront.net domain from distribution-domain.txt).
set -uo pipefail
cd "$(dirname "$0")"

HOST="${1:-$(cat distribution-domain.txt)}"

check() {
  local path="$1" expect="$2"
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' "https://$HOST$path")
  printf '%-28s -> %-4s (expect %s)\n' "$path" "$code" "$expect"
}

echo "Smoke-testing https://$HOST"
check "/"            "403 MissingKey (signed-cookie gate on default behavior)"
check "/media/x.m4a" "403 MissingKey (signed-cookie gate)"
check "/api/health"  "VPS response once vps.shizzle.systems resolves publicly; 502 before that"
check "/ws/"         "VPS response once vps.shizzle.systems resolves publicly; 502 before that"
