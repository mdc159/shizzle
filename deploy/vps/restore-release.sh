#!/usr/bin/env bash
set -euo pipefail

TARGET_SHA=${1:?usage: restore-release.sh TARGET_SHA}
PROD_DIR=${SHIZZLE_PROD_DIR:-/opt/shizzle/prod}
DOCKER_BIN=${SHIZZLE_DOCKER_BIN:-docker}

[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "TARGET_SHA must be exactly 40 lowercase hexadecimal characters" >&2; exit 2; }
PROD_DIR=$(readlink -f -- "$PROD_DIR") || { echo "Cannot resolve production directory" >&2; exit 2; }
case "$PROD_DIR" in
  ""|/) echo "Refusing unsafe production directory: $PROD_DIR" >&2; exit 2 ;;
esac
cd "$PROD_DIR"
umask 077

ROLLBACK_ARCHIVE=.rollback-release.tar.gz
ROLLBACK_TARGET=.rollback-release-target
ROLLBACK_DB_REV=.rollback-db-revision
DEPLOY_PHASE=.deploy-phase
for required_path in "$ROLLBACK_ARCHIVE" "$ROLLBACK_TARGET" "$ROLLBACK_DB_REV" "$DEPLOY_PHASE"; do
  test -f "$required_path" || { echo "Rollback transaction is incomplete: missing $required_path" >&2; exit 1; }
done
test "$(cat "$ROLLBACK_TARGET")" = "$TARGET_SHA" || { echo "Rollback target does not match $TARGET_SHA" >&2; exit 1; }
PREV_DB_REV=$(cat "$ROLLBACK_DB_REV")
PHASE=$(cat "$DEPLOY_PHASE")
if ! [[ "$PREV_DB_REV" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "Invalid rollback database revision: $PREV_DB_REV" >&2
  exit 1
fi

RESTORE_DIR=$(mktemp -d "$PROD_DIR/.restore.XXXXXX")
trap 'rm -rf -- "$RESTORE_DIR"' EXIT
tar -xzf "$ROLLBACK_ARCHIVE" -C "$RESTORE_DIR"
for required_path in .env compose.prod.yml Caddyfile player; do
  test -e "$RESTORE_DIR/$required_path" || { echo "Rollback snapshot is incomplete: missing $required_path" >&2; exit 1; }
done

case "$PHASE" in
  migration-started|services-starting)
    "$DOCKER_BIN" compose -p shizzle -f compose.prod.yml stop api orchestrator
    "$DOCKER_BIN" compose -p shizzle -f compose.prod.yml run --rm --no-deps api \
      alembic downgrade "$PREV_DB_REV"
    ;;
  snapshot-ready|files-activating) ;;
  *) echo "Unknown deployment phase: $PHASE" >&2; exit 1 ;;
esac

rm -rf -- .env.restore compose.prod.yml.restore Caddyfile.restore
cp -a "$RESTORE_DIR/.env" .env.restore
cp -a "$RESTORE_DIR/compose.prod.yml" compose.prod.yml.restore
cp -a "$RESTORE_DIR/Caddyfile" Caddyfile.restore
mv -f .env.restore .env
mv -f compose.prod.yml.restore compose.prod.yml
mv -f Caddyfile.restore Caddyfile
if [ -e player ] || [ -L player ]; then
  rm -rf -- player.failed
  mv player player.failed
fi
mv "$RESTORE_DIR/player" player
rm -rf -- player.failed
"$DOCKER_BIN" compose -p shizzle -f compose.prod.yml up -d --force-recreate --remove-orphans
echo "Restored the previous release at database revision $PREV_DB_REV"
