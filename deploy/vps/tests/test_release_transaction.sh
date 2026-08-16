#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
DEPLOY="$ROOT/deploy/vps/deploy-release.sh"
RESTORE="$ROOT/deploy/vps/restore-release.sh"
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
BIN="$TMP/bin"
mkdir -p "$BIN"

cat > "$BIN/docker" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$DOCKER_LOG"
if [ "${1:-}" = pull ]; then
  [ "${DOCKER_FAIL:-}" != pull ] || exit 31
  exit 0
fi
if [ "${1:-}" != compose ]; then exit 0; fi
case " $* " in
  *" exec -T postgres "*) printf '%s\n' 0004; exit 0 ;;
  *" config -q "*) exit 0 ;;
  *" alembic upgrade head "*) [ "${DOCKER_FAIL:-}" != migration ] || exit 32; exit 0 ;;
  *" alembic downgrade "*) [ "${DOCKER_FAIL:-}" != downgrade ] || exit 33; exit 0 ;;
  *" up -d "*) [ "${DOCKER_FAIL:-}" != up ] || exit 34; exit 0 ;;
esac
exit 0
STUB
chmod +x "$BIN/docker"

TARGET=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
DIGEST=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
IMAGE="ghcr.io/mdc159/shizzle/api:sha-$TARGET@sha256:$DIGEST"

new_fixture() {
  PROD="$TMP/prod-$1"
  mkdir -p "$PROD/player" "$PROD/incoming/dist"
  printf 'POSTGRES_USER=shizzle\nPOSTGRES_DB=shizzle\nSHIZZLE_API_TAG=sha-old\n' > "$PROD/.env"
  printf 'old-compose\n' > "$PROD/compose.prod.yml"
  printf 'old-caddy\n' > "$PROD/Caddyfile"
  printf 'old-player\n' > "$PROD/player/index.html"
  printf 'new-compose\n' > "$PROD/incoming/compose.prod.yml"
  printf 'new-caddy\n' > "$PROD/incoming/Caddyfile.prod"
  printf 'new-player\n' > "$PROD/incoming/dist/index.html"
  tar -C "$PROD/incoming" -czf "$PROD/incoming/shizzle-player.tar.gz" dist
  DOCKER_LOG="$PROD/docker.log"
  : > "$DOCKER_LOG"
  export PROD DOCKER_LOG
}

run_deploy() {
  PATH="$BIN:$PATH" SHIZZLE_PROD_DIR="$PROD" SHIZZLE_DOCKER_BIN=docker \
    DOCKER_LOG="$DOCKER_LOG" DOCKER_FAIL="${DOCKER_FAIL:-}" \
    bash "$DEPLOY" "$IMAGE" "$TARGET"
}

run_restore() {
  PATH="$BIN:$PATH" SHIZZLE_PROD_DIR="$PROD" SHIZZLE_DOCKER_BIN=docker \
    DOCKER_LOG="$DOCKER_LOG" DOCKER_FAIL="${DOCKER_FAIL:-}" \
    bash "$RESTORE" "$TARGET"
}

new_fixture success
run_deploy
grep -Fx "SHIZZLE_API_IMAGE=$IMAGE" "$PROD/.env"
grep -Fx new-compose "$PROD/compose.prod.yml"
grep -Fx new-player "$PROD/player/index.html"
grep -F 'alembic upgrade head' "$DOCKER_LOG"

new_fixture missing-identity
sed -i '/SHIZZLE_API_TAG=/d' "$PROD/.env"
if run_deploy; then echo "missing identity unexpectedly deployed" >&2; exit 1; fi
grep -Fx old-compose "$PROD/compose.prod.yml"
test ! -e "$PROD/.rollback-release.tar.gz"

new_fixture pull-failure
DOCKER_FAIL=pull
if run_deploy; then echo "pull failure unexpectedly deployed" >&2; exit 1; fi
DOCKER_FAIL=
run_restore
grep -Fx old-compose "$PROD/compose.prod.yml"
grep -Fx old-player "$PROD/player/index.html"

new_fixture migration-failure
DOCKER_FAIL=migration
if run_deploy; then echo "migration failure unexpectedly deployed" >&2; exit 1; fi
DOCKER_FAIL=
run_restore
grep -F 'alembic downgrade 0004' "$DOCKER_LOG"
grep -F 'stop api orchestrator' "$DOCKER_LOG"
grep -Fx old-compose "$PROD/compose.prod.yml"

new_fixture health-failure
run_deploy
run_restore
grep -F 'alembic downgrade 0004' "$DOCKER_LOG"
grep -Fx old-caddy "$PROD/Caddyfile"
grep -Fx old-player "$PROD/player/index.html"

printf 'release transaction scenarios passed\n'
