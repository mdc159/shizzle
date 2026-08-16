#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
SCRIPT="$ROOT/deploy/runpod/repoint.sh"
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
BIN="$TMP/bin"
mkdir -p "$BIN"

cat > "$BIN/docker" <<'STUB'
#!/usr/bin/env bash
[ "${MANIFEST_EXIT:-0}" -eq 0 ] || exit "$MANIFEST_EXIT"
printf '%s\n' '{"digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}'
STUB
cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$RUNPOD_LOG"
url=${!#}
case "$url" in
  */endpoints/endpoint-old)
    if [ -f "$RUNPOD_STATE" ] && [ "${RUNPOD_RECONCILE:-}" = incomplete ]; then
      printf '%s\n' '{}'
    elif [ -f "$RUNPOD_STATE" ]; then
      printf '%s\n' '{"id":"endpoint-old","templateId":"template-new","workersMax":4}'
    else
      printf '%s\n' '{"id":"endpoint-old","templateId":"template-old","workersMax":2}'
    fi ;;
  */templates/template-old?includeEndpointBoundTemplates=true)
    printf '%s\n' '{"id":"template-old","name":"old","imageName":"old:image","isServerless":true,"containerDiskInGb":10,"volumeInGb":0,"env":{}}' ;;
  */templates)
    printf '%s\n' '{"id":"template-new","imageName":"ghcr.io/mdc159/shizzle/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}' ;;
  */endpoints/endpoint-old/update)
    [ "${RUNPOD_FAIL:-}" != endpoint ] || exit 22
    if [ "${RUNPOD_FAIL:-}" = postcommit ]; then touch "$RUNPOD_STATE"; exit 22; fi
    printf '%s\n' '{"id":"endpoint-old","templateId":"template-new","workersMax":4}' ;;
  */templates/template-new)
    exit 0 ;;
  *) echo "unexpected fake API URL: $url" >&2; exit 23 ;;
esac
STUB
chmod +x "$BIN/docker" "$BIN/curl"

TAG=sha-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
RUNPOD_LOG="$TMP/runpod.log"
RUNPOD_STATE="$TMP/runpod.state"
export RUNPOD_LOG RUNPOD_STATE
: > "$RUNPOD_LOG"

run_repoint() {
  PATH="$BIN:$PATH" RUNPOD_API_KEY=test RUNPOD_API_BASE=https://fake/v1 \
    RUNPOD_CURL_BIN=curl RUNPOD_DOCKER_BIN=docker GITHUB_RUN_ID=1 \
    RUNPOD_FAIL="${RUNPOD_FAIL:-}" RUNPOD_RECONCILE="${RUNPOD_RECONCILE:-}" \
    MANIFEST_EXIT="${MANIFEST_EXIT:-0}" bash "$SCRIPT" "$@"
}

if run_repoint bad-tag 4 endpoint-old template-old; then
  echo "invalid tag unexpectedly reached the API" >&2
  exit 1
fi
test ! -s "$RUNPOD_LOG"

MANIFEST_EXIT=1
if run_repoint "$TAG" 4 endpoint-old template-old; then
  echo "missing worker image unexpectedly reached the API" >&2
  exit 1
fi
MANIFEST_EXIT=0
test ! -s "$RUNPOD_LOG"

if run_repoint "$TAG" 4 '../endpoint' template-old; then
  echo "invalid endpoint unexpectedly reached the API" >&2
  exit 1
fi
test ! -s "$RUNPOD_LOG"

run_repoint "$TAG" 4 endpoint-old template-old > "$TMP/result.json"
jq -e '.templateId == "template-new" and .workersMax == 4' "$TMP/result.json" >/dev/null
grep -F 'ghcr.io/mdc159/shizzle/worker@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' "$TMP/result.json"
grep -F '/templates' "$RUNPOD_LOG"
grep -F '/endpoints/endpoint-old/update' "$RUNPOD_LOG"
if grep -F -- '-X DELETE' "$RUNPOD_LOG"; then
  echo "successful update deleted its bound template" >&2
  exit 1
fi

: > "$RUNPOD_LOG"
rm -f "$RUNPOD_STATE"
RUNPOD_FAIL=postcommit
RUNPOD_RECONCILE=incomplete
if run_repoint "$TAG" 4 endpoint-old template-old; then
  echo "incomplete reconciliation unexpectedly succeeded" >&2
  exit 1
fi
if grep -F -- '-X DELETE' "$RUNPOD_LOG"; then
  echo "incomplete reconciliation deleted a potentially bound template" >&2
  exit 1
fi

: > "$RUNPOD_LOG"
rm -f "$RUNPOD_STATE"
RUNPOD_FAIL=endpoint
RUNPOD_RECONCILE=
if run_repoint "$TAG" 4 endpoint-old template-old; then
  echo "failed endpoint update unexpectedly succeeded" >&2
  exit 1
fi
grep -F -- '-X DELETE' "$RUNPOD_LOG"
grep -F '/templates/template-new' "$RUNPOD_LOG"

: > "$RUNPOD_LOG"
rm -f "$RUNPOD_STATE"
RUNPOD_FAIL=postcommit
RUNPOD_RECONCILE=
if run_repoint "$TAG" 4 endpoint-old template-old; then
  echo "lost response unexpectedly succeeded" >&2
  exit 1
fi
grep -F '/endpoints/endpoint-old' "$RUNPOD_LOG"
if grep -F -- '-X DELETE' "$RUNPOD_LOG"; then
  echo "ambiguous committed update deleted the bound template" >&2
  exit 1
fi

printf 'RunPod repoint transaction scenarios passed\n'
