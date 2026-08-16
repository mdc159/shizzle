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
exit "${MANIFEST_EXIT:-0}"
STUB
cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$RUNPOD_LOG"
url=${!#}
case "$url" in
  */endpoints/endpoint-old)
    printf '%s\n' '{"id":"endpoint-old","templateId":"template-old","workersMax":2}' ;;
  */templates/template-old)
    printf '%s\n' '{"id":"template-old","name":"old","imageName":"old:image","isServerless":true,"containerDiskInGb":10,"volumeInGb":0,"env":{}}' ;;
  */templates)
    printf '%s\n' '{"id":"template-new","imageName":"ghcr.io/mdc159/shizzle/worker:sha-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}' ;;
  */endpoints/endpoint-old/update)
    [ "${RUNPOD_FAIL:-}" != endpoint ] || exit 22
    printf '%s\n' '{"id":"endpoint-old","templateId":"template-new","workersMax":4}' ;;
  */templates/template-new)
    exit 0 ;;
  *) echo "unexpected fake API URL: $url" >&2; exit 23 ;;
esac
STUB
chmod +x "$BIN/docker" "$BIN/curl"

TAG=sha-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
RUNPOD_LOG="$TMP/runpod.log"
export RUNPOD_LOG
: > "$RUNPOD_LOG"

run_repoint() {
  PATH="$BIN:$PATH" RUNPOD_API_KEY=test RUNPOD_API_BASE=https://fake/v1 \
    RUNPOD_CURL_BIN=curl RUNPOD_DOCKER_BIN=docker GITHUB_RUN_ID=1 \
    RUNPOD_FAIL="${RUNPOD_FAIL:-}" bash "$SCRIPT" "$@"
}

if run_repoint bad-tag 4 endpoint-old template-old; then
  echo "invalid tag unexpectedly reached the API" >&2
  exit 1
fi
test ! -s "$RUNPOD_LOG"

run_repoint "$TAG" 4 endpoint-old template-old > "$TMP/result.json"
jq -e '.templateId == "template-new" and .workersMax == 4' "$TMP/result.json" >/dev/null
grep -F '/templates' "$RUNPOD_LOG"
grep -F '/endpoints/endpoint-old/update' "$RUNPOD_LOG"
if grep -F -- '-X DELETE' "$RUNPOD_LOG"; then
  echo "successful update deleted its bound template" >&2
  exit 1
fi

: > "$RUNPOD_LOG"
RUNPOD_FAIL=endpoint
if run_repoint "$TAG" 4 endpoint-old template-old; then
  echo "failed endpoint update unexpectedly succeeded" >&2
  exit 1
fi
grep -F -- '-X DELETE' "$RUNPOD_LOG"
grep -F '/templates/template-new' "$RUNPOD_LOG"

printf 'RunPod repoint transaction scenarios passed\n'
