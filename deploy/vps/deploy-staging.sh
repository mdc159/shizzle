#!/usr/bin/env bash
# Staging release transaction: pull the digest-pinned image, validate the
# staging Compose config, run alembic upgrade head from the new image, start,
# and health check on 127.0.0.1:18080. Same shape as deploy-release.sh, but
# with a simpler rollback: staging holds nothing precious, so a failure
# restores the prior files and either restarts the previous image or stops
# the application services — the database is never downgraded.
#
# This script must be impossible to aim at production: it refuses to run when
# its target directory is (or is inside) the production directory, or when
# the Compose project name is `shizzle`.
set -Eeuo pipefail # -E: the ERR trap below fires for failures inside functions too

IMAGE_REF=${1:?usage: deploy-staging.sh IMAGE_REF TARGET_SHA}
TARGET_SHA=${2:?usage: deploy-staging.sh IMAGE_REF TARGET_SHA}
STAGING_DIR=${SHIZZLE_STAGING_DIR:-/opt/shizzle/staging}
STAGING_PROJECT=${SHIZZLE_STAGING_PROJECT:-shizzle-staging}
PROD_DIR_GUARD=${SHIZZLE_STAGING_PROD_DIR:-/opt/shizzle/prod}
DOCKER_BIN=${SHIZZLE_DOCKER_BIN:-docker}
CURL_BIN=${SHIZZLE_CURL_BIN:-curl}
HEALTH_URL=${SHIZZLE_STAGING_HEALTH_URL:-http://127.0.0.1:18080/api/health}
HEALTH_ATTEMPTS=${SHIZZLE_STAGING_HEALTH_ATTEMPTS:-12}
HEALTH_SLEEP=${SHIZZLE_STAGING_HEALTH_SLEEP:-10}

STAGING_DIR=$(readlink -f -- "$STAGING_DIR") || { echo "Cannot resolve staging directory" >&2; exit 2; }
case "$STAGING_DIR" in
  ""|/) echo "Refusing unsafe staging directory: $STAGING_DIR" >&2; exit 2 ;;
esac
PROD_DIR_GUARD=$(readlink -f -- "$PROD_DIR_GUARD" 2>/dev/null) || PROD_DIR_GUARD=""
case "$STAGING_DIR" in
  "$PROD_DIR_GUARD"|"$PROD_DIR_GUARD"/*)
    echo "Refusing to operate on the production directory: $STAGING_DIR" >&2
    exit 2
    ;;
esac
case "$STAGING_PROJECT" in
  ""|shizzle) echo "Refusing production Compose project name: $STAGING_PROJECT" >&2; exit 2 ;;
esac
[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "TARGET_SHA must be exactly 40 lowercase hexadecimal characters" >&2
  exit 2
}
[[ "$IMAGE_REF" =~ ^ghcr\.io/mdc159/shizzle/api:sha-${TARGET_SHA}@sha256:[0-9a-f]{64}$ ]] || {
  echo "IMAGE_REF must pin the target SHA tag and a sha256 digest" >&2
  exit 2
}
command -v "$CURL_BIN" >/dev/null 2>&1 || {
  echo "curl is required for the staging health check" >&2
  exit 2
}

cd "$STAGING_DIR"
umask 077
for required_path in .env compose.staging.yml Caddyfile player incoming/compose.staging.yml incoming/Caddyfile.staging incoming/shizzle-player.tar.gz; do
  if [ ! -e "$required_path" ]; then
    echo "Cannot deploy: missing $required_path" >&2
    exit 1
  fi
done

compose() {
  "$DOCKER_BIN" compose -p "$STAGING_PROJECT" -f compose.staging.yml "$@"
}

PREV_IMAGE=$(sed -n 's/^SHIZZLE_API_IMAGE=//p' .env | tail -n 1)
ROLLBACK_ARCHIVE=.staging-rollback.tar.gz
STAGE_DIR=$(mktemp -d "$STAGING_DIR/.staging-stage.XXXXXX")
trap 'rm -rf -- "$STAGE_DIR"; rm -f -- "$ROLLBACK_ARCHIVE.tmp"' EXIT

# Restore the snapshot taken below and bring back the previous release. The
# database is left as migrated: staging holds nothing precious, and an old
# image against a newer schema is acceptable there (redeploy the newer ref).
restore_previous() {
  echo "Staging deploy failed; restoring the prior staging files" >&2
  if [ ! -f "$ROLLBACK_ARCHIVE" ]; then
    echo "No rollback snapshot to restore" >&2
    return 0
  fi
  local restore_dir
  restore_dir=$(mktemp -d "$STAGING_DIR/.staging-restore.XXXXXX") || return 0
  tar -xzf "$ROLLBACK_ARCHIVE" -C "$restore_dir" 2>/dev/null || true
  local f
  for f in .env compose.staging.yml Caddyfile; do
    if [ -e "$restore_dir/$f" ]; then
      mv -f -- "$restore_dir/$f" "$f"
    fi
  done
  if [ -e "$restore_dir/player" ]; then
    if [ -e player ] || [ -L player ]; then
      rm -rf -- player.failed
      mv -- player player.failed
    fi
    mv -- "$restore_dir/player" player
    rm -rf -- player.failed
  fi
  rm -rf -- "$restore_dir"
  if [ -n "$PREV_IMAGE" ]; then
    compose up -d --force-recreate --no-deps api orchestrator caddy >&2 || true
  else
    compose stop api orchestrator caddy >&2 || true
  fi
  rm -f -- "$ROLLBACK_ARCHIVE"
}

on_error() {
  status=$?
  trap - ERR
  restore_previous
  exit "$status"
}
trap on_error ERR

# Snapshot the prior release (also proves there is a real one to restore).
tar -czf "$ROLLBACK_ARCHIVE.tmp" -- .env compose.staging.yml Caddyfile player
mv -f "$ROLLBACK_ARCHIVE.tmp" "$ROLLBACK_ARCHIVE"

"$DOCKER_BIN" pull "$IMAGE_REF"
tar -xzf incoming/shizzle-player.tar.gz -C "$STAGE_DIR"
test -d "$STAGE_DIR/dist"
install -m 600 .env "$STAGE_DIR/.env"
sed -i '/^SHIZZLE_API_TAG=/d; /^SHIZZLE_API_IMAGE=/d' "$STAGE_DIR/.env"
printf 'SHIZZLE_API_IMAGE=%s\n' "$IMAGE_REF" >> "$STAGE_DIR/.env"
install -m 644 incoming/compose.staging.yml "$STAGE_DIR/compose.staging.yml"
install -m 644 incoming/Caddyfile.staging "$STAGE_DIR/Caddyfile"
mv "$STAGE_DIR/dist" "$STAGE_DIR/player"

SHIZZLE_API_IMAGE=$IMAGE_REF "$DOCKER_BIN" compose \
  --env-file "$STAGE_DIR/.env" -p "$STAGING_PROJECT" -f "$STAGE_DIR/compose.staging.yml" config -q

mv -f "$STAGE_DIR/.env" .env
mv -f "$STAGE_DIR/compose.staging.yml" compose.staging.yml
mv -f "$STAGE_DIR/Caddyfile" Caddyfile
rm -rf -- player.next
mv "$STAGE_DIR/player" player.next
rm -rf -- player
mv player.next player

# --wait blocks until postgres is healthy (first bootstrap included).
compose up -d --wait postgres
compose run --rm --no-deps api alembic upgrade head
compose up -d --force-recreate --remove-orphans --no-deps api orchestrator caddy

check_health() {
  local attempt body normalized
  for attempt in $(seq 1 "$HEALTH_ATTEMPTS"); do
    if body=$("$CURL_BIN" -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null); then
      normalized=$(printf '%s' "$body" | tr -d ' \n\t')
      if printf '%s' "$normalized" | grep -q '"status":"ok"' \
        && printf '%s' "$normalized" | grep -q '"db":true' \
        && printf '%s' "$normalized" | grep -q '"orchestratorAlive":true'; then
        return 0
      fi
    fi
    echo "Staging health check attempt $attempt failed on $HEALTH_URL; retrying" >&2
    sleep "$HEALTH_SLEEP"
  done
  echo "Staging health check failed on $HEALTH_URL" >&2
  return 1
}
check_health

rm -f -- "$ROLLBACK_ARCHIVE"
echo "Activated staging $IMAGE_REF"
