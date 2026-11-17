#!/usr/bin/env bash
# Staging release transaction: pull the digest-pinned image, validate the
# staging Compose config, run alembic upgrade head from the new image, start,
# and health check on 127.0.0.1:18080. Same shape as deploy-release.sh, but
# with a simpler rollback: staging holds nothing precious, so a failure stops
# the failed services while their config is still active, then restores the
# prior files and either restarts the previous image or returns the directory
# to its fresh state — the database is never downgraded.
#
# First deployment (fresh directory: .env plus incoming/ assets, no prior
# compose.staging.yml/Caddyfile/player) is handled explicitly: only the
# files that exist are snapshotted, and a failed first deploy removes what it
# activated so the next run is a fresh deploy again.
#
# This script must be impossible to aim at production: it ALWAYS refuses the
# canonical /opt/shizzle/prod (and anything under it) and the project name
# `shizzle`, whatever the environment says. SHIZZLE_STAGING_PROD_DIR can only
# add one more forbidden path (used by the tests); it can never remove the
# built-in one, and an unresolvable extra guard fails closed.
set -Eeuo pipefail # -E: the ERR trap below fires for failures inside functions too

IMAGE_REF=${1:?usage: deploy-staging.sh IMAGE_REF TARGET_SHA}
TARGET_SHA=${2:?usage: deploy-staging.sh IMAGE_REF TARGET_SHA}
STAGING_DIR=${SHIZZLE_STAGING_DIR:-/opt/shizzle/staging}
STAGING_PROJECT=${SHIZZLE_STAGING_PROJECT:-shizzle-staging}
EXTRA_GUARD_DIR=${SHIZZLE_STAGING_PROD_DIR:-}
DOCKER_BIN=${SHIZZLE_DOCKER_BIN:-docker}
CURL_BIN=${SHIZZLE_CURL_BIN:-curl}
HEALTH_URL=${SHIZZLE_STAGING_HEALTH_URL:-http://127.0.0.1:18080/api/health}
HEALTH_ATTEMPTS=${SHIZZLE_STAGING_HEALTH_ATTEMPTS:-12}
HEALTH_SLEEP=${SHIZZLE_STAGING_HEALTH_SLEEP:-10}

# Unconditional production guard, part 1: refuse the literal path before any
# resolution, so nothing downstream (including a filesystem where /opt is
# absent) can influence this refusal. No environment variable can remove or
# replace it.
case "$STAGING_DIR" in
  /opt/shizzle/prod|/opt/shizzle/prod/*)
    echo "Refusing to operate on the production directory: $STAGING_DIR" >&2
    exit 2
    ;;
esac
STAGING_DIR=$(readlink -f -- "$STAGING_DIR") || { echo "Cannot resolve staging directory; failing closed" >&2; exit 2; }
case "$STAGING_DIR" in
  ""|/) echo "Refusing unsafe staging directory: $STAGING_DIR" >&2; exit 2 ;;
esac
# Unconditional production guard, part 2: the canonical path (catches symlink
# aliases and ../ spellings of the same production directory).
case "$STAGING_DIR" in
  /opt/shizzle/prod|/opt/shizzle/prod/*)
    echo "Refusing to operate on the production directory: $STAGING_DIR" >&2
    exit 2
    ;;
esac
# Optional additional forbidden path (tests use it to guard their fixtures).
# It can only add a refusal, and an unresolvable path fails closed.
if [ -n "$EXTRA_GUARD_DIR" ]; then
  EXTRA_GUARD_DIR=$(readlink -f -- "$EXTRA_GUARD_DIR") || {
    echo "Cannot resolve the additional guard directory; failing closed" >&2
    exit 2
  }
  case "$STAGING_DIR" in
    "$EXTRA_GUARD_DIR"|"$EXTRA_GUARD_DIR"/*)
      echo "Refusing to operate on the production directory: $STAGING_DIR" >&2
      exit 2
      ;;
  esac
fi
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
# Always required: the private environment and the incoming release assets.
for required_path in .env incoming/compose.staging.yml incoming/Caddyfile.staging incoming/shizzle-player.tar.gz; do
  if [ ! -e "$required_path" ]; then
    echo "Cannot deploy: missing $required_path" >&2
    exit 1
  fi
done

compose() {
  "$DOCKER_BIN" compose -p "$STAGING_PROJECT" -f compose.staging.yml "$@"
}

# Owner's rule: no symlinks, anywhere. Refuse any archive that carries a
# member that is not a regular file or directory (symlink, hard link, device,
# fifo, socket) — `test -d` follows links and would accept a dist that is a
# link to somewhere it should never point.
validate_archive_member_types() {
  local archive=$1 bad
  if ! tar -tzf "$archive" >/dev/null 2>&1; then
    echo "Refusing archive $archive: not a readable tar.gz" >&2
    return 1
  fi
  bad=$(tar -tvzf "$archive" | awk '{print substr($1, 1, 1)}' | grep -vE '^[d-]$' | sort -u | tr '\n' ' ' || true)
  if [ -n "$bad" ]; then
    echo "Refusing archive $archive: non-regular member types present: $bad" >&2
    return 1
  fi
}

# The player archive additionally must contain only dist/ at the top level
# (this also rejects absolute and '..' member paths), and that top level must
# be a real directory.
validate_player_archive() {
  local archive=$1 bad
  validate_archive_member_types "$archive" || return 1
  bad=$(tar -tzf "$archive" | grep -vE '^dist/' | head -n 3 | tr '\n' ' ' || true)
  if [ -n "$bad" ]; then
    echo "Refusing player archive: members outside dist/ (absolute path, '..', or wrong top level): $bad" >&2
    return 1
  fi
  tar -tvzf "$archive" | grep -Eq '^d[^ ]*[[:space:]].*[[:space:]]dist/$' \
    || { echo "Refusing player archive: top-level entry is not a real dist/ directory" >&2; return 1; }
}

PREV_IMAGE=$(sed -n 's/^SHIZZLE_API_IMAGE=//p' .env | tail -n 1)
ACTIVATED=0
if [ -z "$PREV_IMAGE" ]; then
  # Fresh directory: a first deploy. Prior release files must not exist; a
  # half-activated tree without a recorded image identity is refused rather
  # than guessed at.
  for stale_path in compose.staging.yml Caddyfile player; do
    if [ -e "$stale_path" ] || [ -L "$stale_path" ]; then
      echo "Refusing first deployment with stale release file present: $stale_path (no SHIZZLE_API_IMAGE recorded in .env)" >&2
      exit 1
    fi
  done
  SNAPSHOT_PATHS=".env"
else
  for required_path in compose.staging.yml Caddyfile player; do
    if [ ! -e "$required_path" ]; then
      echo "Cannot deploy: missing $required_path" >&2
      exit 1
    fi
  done
  SNAPSHOT_PATHS=".env compose.staging.yml Caddyfile player"
fi

# Validate the incoming player archive BEFORE anything is created or
# snapshotted: a refusal here leaves the staging directory untouched.
validate_player_archive incoming/shizzle-player.tar.gz

ROLLBACK_ARCHIVE=.staging-rollback.tar.gz
STAGE_DIR=$(mktemp -d "$STAGING_DIR/.staging-stage.XXXXXX")
trap 'rm -rf -- "$STAGE_DIR"; rm -f -- "$ROLLBACK_ARCHIVE.tmp"' EXIT

# Stop the failed services and restore the prior state. Order matters: the
# stop must run while the FAILED release's config (.env with its image
# identity) is still in place — restoring the previous .env first (e.g. one
# without an image on a first deploy) would make Compose interpolation fail
# before anything could be stopped. A stop failure is surfaced, not swallowed.
restore_previous() {
  echo "Staging deploy failed; rolling back" >&2
  local stop_status=0 restore_dir f
  if [ "$ACTIVATED" -eq 1 ]; then
    compose stop api orchestrator caddy >&2 || stop_status=$?
  fi
  restore_dir=$(mktemp -d "$STAGING_DIR/.staging-restore.XXXXXX") || return 0
  # The rollback archive is unpacked with the same refusal as the player
  # archive: if the snapshot fails validation, do not restore it — surface
  # the failure and leave manual recovery.
  validate_archive_member_types "$ROLLBACK_ARCHIVE" || {
    echo "Rollback snapshot failed member validation; leaving the failed release in place for manual recovery" >&2
    rm -rf -- "$restore_dir"
    return 1
  }
  tar -xzf "$ROLLBACK_ARCHIVE" -C "$restore_dir" --no-same-owner --no-same-permissions 2>/dev/null || true
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
  if [ -z "$PREV_IMAGE" ]; then
    # Failed first deploy: return the directory to its fresh state so the
    # next run is a first deploy again (no image identity, no release files).
    rm -rf -- compose.staging.yml Caddyfile player
  else
    compose up -d --force-recreate --no-deps api orchestrator caddy >&2 || true
  fi
  rm -f -- "$ROLLBACK_ARCHIVE"
  if [ "$stop_status" -ne 0 ]; then
    echo "Rollback could not stop the failed services (status $stop_status); they may still be running" >&2
    return "$stop_status"
  fi
  return 0
}

on_error() {
  status=$?
  trap - ERR
  if [ -f "$ROLLBACK_ARCHIVE" ]; then
    restore_previous || status=$?
  fi
  exit "$status"
}
trap on_error ERR

# Snapshot the prior state (only the release files that exist).
tar -czf "$ROLLBACK_ARCHIVE.tmp" -- $SNAPSHOT_PATHS
mv -f "$ROLLBACK_ARCHIVE.tmp" "$ROLLBACK_ARCHIVE"

"$DOCKER_BIN" pull "$IMAGE_REF"
# Extract without preserving owners or special modes, and verify on disk what
# the listing check proved about the archive.
tar -xzf incoming/shizzle-player.tar.gz -C "$STAGE_DIR" --no-same-owner --no-same-permissions
if [ ! -d "$STAGE_DIR/dist" ] || [ -L "$STAGE_DIR/dist" ]; then
  echo "Refusing player archive: dist is not a plain directory after extraction" >&2
  exit 1
fi
bad_on_disk=$(find "$STAGE_DIR/dist" \( ! -type f -a ! -type d \) -print -quit)
if [ -n "$bad_on_disk" ]; then
  echo "Refusing player archive: non-regular entry on disk: $bad_on_disk" >&2
  exit 1
fi
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
ACTIVATED=1

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
