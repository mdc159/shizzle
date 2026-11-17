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
  # awk consumes the complete listing; no consumer exits early, so a valid
  # large archive cannot die of SIGPIPE (statuses 141 0) under pipefail.
  bad=$(tar -tvzf "$archive" | awk 'substr($1, 1, 1) != "d" && substr($1, 1, 1) != "-" { print substr($1, 1, 1) }' | sort -u | tr '\n' ' ')
  if [ -n "$bad" ]; then
    echo "Refusing archive $archive: non-regular member types present: $bad" >&2
    return 1
  fi
}

# Shared path validation: reject absolute member paths and every member
# containing a '..' path component (including dist/../evil and deeper
# variants, which a plain top-level prefix check would let through).
# Applied to the player archive and to the rollback snapshot alike.
validate_archive_paths() {
  local archive=$1 verdict
  verdict=$(tar -tzf "$archive" | awk '
    /^\// { absolute = 1 }
    {
      n = split($0, comp, "/")
      for (i = 1; i <= n; i++) if (comp[i] == "..") dotdot = 1
    }
    END {
      if (absolute) print "absolute"
      else if (dotdot) print "dotdot"
      else print "ok"
    }
  ')
  case "$verdict" in
    ok) return 0 ;;
    absolute) echo "Refusing archive $archive: absolute member paths present" >&2; return 1 ;;
    dotdot) echo "Refusing archive $archive: '..' path components present" >&2; return 1 ;;
    *) echo "Refusing archive $archive: unreadable listing" >&2; return 1 ;;
  esac
}

# The player archive additionally must contain only dist/ at the top level
# and that top level must be a real directory. The awk pass reads the WHOLE
# listing (an early-exiting grep -q under pipefail turns a valid large
# archive into 141 0 and a false refusal).
validate_player_archive() {
  local archive=$1 verdict
  validate_archive_member_types "$archive" || return 1
  validate_archive_paths "$archive" || return 1
  verdict=$(tar -tvzf "$archive" | awk '
    {
      # Fields: perms owner/group size date time name... (rebuild the name so
      # spaces survive).
      name = $6
      for (i = 7; i <= NF; i++) name = name " " $i
      if (name !~ /^dist\//) outside = 1
      if (substr($1, 1, 1) == "d" && name == "dist/") have_dist = 1
    }
    END {
      if (outside) print "outside"
      else if (!have_dist) print "no-dist-dir"
      else print "ok"
    }
  ')
  case "$verdict" in
    ok) return 0 ;;
    outside)
      echo "Refusing player archive: members outside dist/ (wrong top level)" >&2
      return 1
      ;;
    no-dist-dir)
      echo "Refusing player archive: top-level entry is not a real dist/ directory" >&2
      return 1
      ;;
    *)
      echo "Refusing player archive: unreadable listing" >&2
      return 1
      ;;
  esac
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
# A prior crash or failed rollback leaves the only trustworthy snapshot of
# the previous release. Never overwrite it (or an incomplete new snapshot)
# and mistake the partially activated tree for a clean prior release.
for stale_artifact in "$ROLLBACK_ARCHIVE" "$ROLLBACK_ARCHIVE.tmp"; do
  if [ -e "$stale_artifact" ] || [ -L "$stale_artifact" ]; then
    echo "Refusing staging deploy while rollback artifact exists: $stale_artifact; recover or explicitly remove it first" >&2
    exit 1
  fi
done
STAGE_DIR=$(mktemp -d "$STAGING_DIR/.staging-stage.XXXXXX")
trap 'rm -rf -- "$STAGE_DIR"; rm -f -- "$ROLLBACK_ARCHIVE.tmp"' EXIT

# Stop the failed services and restore the prior state. Order matters: the
# stop must run while the FAILED release's config (.env with its image
# identity) is still in place — restoring the previous .env first (e.g. one
# without an image on a first deploy) would make Compose interpolation fail
# before anything could be stopped. A stop failure is surfaced, not swallowed.
restore_previous() {
  echo "Staging deploy failed; rolling back" >&2
  local stop_status=0 restart_status=0 restore_dir f moved_old_player=0
  if [ "$ACTIVATED" -eq 1 ]; then
    compose stop api orchestrator caddy >&2 || stop_status=$?
  fi
  restore_dir=$(mktemp -d "$STAGING_DIR/.staging-restore.XXXXXX") || {
    echo "Rollback could not create a restore directory; keeping $ROLLBACK_ARCHIVE and leaving the failed release in place for manual recovery" >&2
    return 1
  }
  # The rollback archive is unpacked with the same refusal as the player
  # archive: if the snapshot fails validation, do not restore it — surface
  # the failure and leave manual recovery.
  validate_archive_member_types "$ROLLBACK_ARCHIVE" || {
    echo "Rollback snapshot failed member validation; leaving the failed release in place for manual recovery" >&2
    rm -rf -- "$restore_dir"
    return 1
  }
  validate_archive_paths "$ROLLBACK_ARCHIVE" || {
    echo "Rollback snapshot failed path validation; leaving the failed release in place for manual recovery" >&2
    rm -rf -- "$restore_dir"
    return 1
  }
  # A failed extraction restores nothing: report and leave manual recovery
  # rather than moving a partial restore into place.
  if ! tar -xzf "$ROLLBACK_ARCHIVE" -C "$restore_dir" --no-same-owner --no-same-permissions; then
    echo "Rollback snapshot failed to extract; leaving the failed release in place for manual recovery" >&2
    rm -rf -- "$restore_dir"
    return 1
  fi
  # This function runs in an || list from on_error, so bash suppresses
  # errexit throughout it. Check every mutation explicitly: an ignored mv/rm
  # must never delete the only rollback archive or report a false success.
  for f in .env compose.staging.yml Caddyfile; do
    if [ -e "$restore_dir/$f" ]; then
      if ! mv -f -- "$restore_dir/$f" "$f"; then
        echo "Rollback failed to restore $f; keeping $ROLLBACK_ARCHIVE for manual recovery" >&2
        rm -rf -- "$restore_dir"
        return 1
      fi
    fi
  done
  if [ -e "$restore_dir/player" ]; then
    if [ -e player ] || [ -L player ]; then
      if ! rm -rf -- player.failed; then
        echo "Rollback could not clear player.failed; keeping $ROLLBACK_ARCHIVE for manual recovery" >&2
        rm -rf -- "$restore_dir"
        return 1
      fi
      if ! mv -- player player.failed; then
        echo "Rollback could not set aside the failed player; keeping $ROLLBACK_ARCHIVE for manual recovery" >&2
        rm -rf -- "$restore_dir"
        return 1
      fi
      moved_old_player=1
    fi
    if ! mv -- "$restore_dir/player" player; then
      echo "Rollback failed to restore player; keeping $ROLLBACK_ARCHIVE for manual recovery" >&2
      if [ "$moved_old_player" -eq 1 ]; then
        mv -- player.failed player || echo "Rollback also could not move player.failed back; manual recovery required" >&2
      fi
      rm -rf -- "$restore_dir"
      return 1
    fi
    if ! rm -rf -- player.failed; then
      echo "Rollback restored player but could not remove player.failed; keeping $ROLLBACK_ARCHIVE for manual recovery" >&2
      rm -rf -- "$restore_dir"
      return 1
    fi
  fi
  if ! rm -rf -- "$restore_dir"; then
    echo "Rollback could not remove its restore directory; keeping $ROLLBACK_ARCHIVE for manual recovery" >&2
    return 1
  fi
  if [ -z "$PREV_IMAGE" ]; then
    # Failed first deploy: return the directory to its fresh state so the
    # next run is a first deploy again (no image identity, no release files).
    if ! rm -rf -- compose.staging.yml Caddyfile player; then
      echo "Rollback could not remove the failed first release; keeping $ROLLBACK_ARCHIVE for manual recovery" >&2
      return 1
    fi
  elif [ "$ACTIVATED" -eq 1 ]; then
    # Before activation the old services were never stopped or replaced;
    # recreating them would be an unnecessary second outage.
    compose up -d --force-recreate --no-deps api orchestrator caddy >&2 || restart_status=$?
    if [ "$restart_status" -ne 0 ]; then
      echo "Rollback restored files but could not restart the previous release (status $restart_status); keeping $ROLLBACK_ARCHIVE for manual recovery" >&2
      return "$restart_status"
    fi
  fi
  if ! rm -f -- "$ROLLBACK_ARCHIVE"; then
    echo "Rollback completed but could not remove $ROLLBACK_ARCHIVE; refusing to hide the stale artifact" >&2
    return 1
  fi
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
# Crafted mode bits could otherwise make subtrees untraversable, and the
# on-disk verification below must see every entry: normalize our own access
# before checking (the eventual owner is root on the box either way).
chmod -R u+rwX "$STAGE_DIR/dist"
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

# /api/health reports the age of the LATEST heartbeat, which can belong to
# the orchestrator that was just replaced. Prove this replacement wrote its
# own row too. Docker's hostname is the 12-character container-ID prefix and
# Orchestrator constructs worker_id as hostname-pid-uuid, so the full
# container ID gives a deployment-specific, unspoofable-by-old-row lookup.
replacement_orchestrator_heartbeat() {
  local container_id query heartbeat
  if ! container_id=$(compose ps -q orchestrator); then
    echo "Could not identify the replacement orchestrator container" >&2
    return 1
  fi
  if [[ ! "$container_id" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Replacement orchestrator returned an invalid container identity" >&2
    return 1
  fi
  query="SELECT EXISTS (SELECT 1 FROM orchestrator_heartbeats WHERE worker_id LIKE '${container_id:0:12}-%');"
  if ! heartbeat=$(compose exec -T postgres sh -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "$1"' sh "$query"); then
    echo "Could not query the replacement orchestrator heartbeat" >&2
    return 1
  fi
  if [ "$heartbeat" != t ]; then
    echo "Replacement orchestrator ${container_id:0:12} has not written its service heartbeat" >&2
    return 1
  fi
}

check_health() {
  local attempt body normalized
  for attempt in $(seq 1 "$HEALTH_ATTEMPTS"); do
    if body=$("$CURL_BIN" -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null); then
      normalized=$(printf '%s' "$body" | tr -d ' \n\t')
      if printf '%s' "$normalized" | grep -q '"status":"ok"' \
        && printf '%s' "$normalized" | grep -q '"db":true' \
        && printf '%s' "$normalized" | grep -q '"orchestratorAlive":true' \
        && replacement_orchestrator_heartbeat; then
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
