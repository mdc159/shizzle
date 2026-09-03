#!/usr/bin/env bash
set -euo pipefail

IMAGE_REF=${1:?usage: deploy-release.sh IMAGE_REF TARGET_SHA}
TARGET_SHA=${2:?usage: deploy-release.sh IMAGE_REF TARGET_SHA}
PROD_DIR=${SHIZZLE_PROD_DIR:-/opt/shizzle/prod}
DOCKER_BIN=${SHIZZLE_DOCKER_BIN:-docker}

PROD_DIR=$(readlink -f -- "$PROD_DIR") || { echo "Cannot resolve production directory" >&2; exit 2; }
case "$PROD_DIR" in
  ""|/) echo "Refusing unsafe production directory: $PROD_DIR" >&2; exit 2 ;;
esac
[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || {
  echo "TARGET_SHA must be exactly 40 lowercase hexadecimal characters" >&2
  exit 2
}
[[ "$IMAGE_REF" =~ ^ghcr\.io/mdc159/shizzle/api:sha-${TARGET_SHA}@sha256:[0-9a-f]{64}$ ]] || {
  echo "IMAGE_REF must pin the target SHA tag and a sha256 digest" >&2
  exit 2
}

cd "$PROD_DIR"
umask 077
for stale_artifact in .rollback-release.tar.gz .rollback-release-target .rollback-db-revision .deploy-phase; do
  if [ -e "$stale_artifact" ] || [ -L "$stale_artifact" ]; then
    echo "Refusing deployment with stale transaction artifact: $stale_artifact" >&2
    exit 1
  fi
done
for required_path in .env compose.prod.yml Caddyfile player incoming/compose.prod.yml incoming/Caddyfile.prod incoming/shizzle-player.tar.gz; do
  if [ ! -e "$required_path" ]; then
    echo "Cannot deploy: missing $required_path" >&2
    exit 1
  fi
done

# E6: the passcode gate is never silently open in production. Fail before
# the transaction when the production .env leaves SHIZZLE_PASSCODE empty or
# missing, unless SHIZZLE_ALLOW_OPEN_GATE=1 is set deliberately.
# Values are read with Compose dotenv semantics — the last assignment wins
# (same as PREV_IMAGE), matching quotes are stripped, and unquoted values
# lose surrounding whitespace and any ` # inline comment` — so a value
# Compose would deliver to the API as empty (`SHIZZLE_PASSCODE=   `,
# `SHIZZLE_PASSCODE=""`, `SHIZZLE_PASSCODE= # blank`) is rejected here,
# before the transaction touches files.
dotenv_value() {
  local key=$1 raw
  raw=$(sed -n "s/^[[:space:]]*\(export[[:space:]]\+\)\{0,1\}${key}[[:space:]]*=[[:space:]]*//p" .env | tail -n 1)
  # Quoted values keep their content verbatim; Compose strips the quotes.
  if [[ "$raw" =~ ^\"(.*)\"[[:space:]]*$ ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
    return
  fi
  if [[ "$raw" =~ ^\'(.*)\'[[:space:]]*$ ]]; then
    printf '%s' "${BASH_REMATCH[1]}"
    return
  fi
  # Unquoted: `#` starts a comment (a leading `#` after `=` or a ` #`
  # inline comment, per Compose dotenv rules); trim surrounding whitespace.
  # A passcode that genuinely starts with `#` must be quoted to survive.
  raw="${raw#"${raw%%[![:space:]]*}"}"
  case "$raw" in
    '#'*) raw= ;;
    *) raw=${raw%%[[:space:]]#*} ;;
  esac
  raw="${raw%"${raw##*[![:space:]]}"}"
  printf '%s' "$raw"
}
PASSCODE_VALUE=$(dotenv_value SHIZZLE_PASSCODE)
OPEN_GATE_VALUE=$(dotenv_value SHIZZLE_ALLOW_OPEN_GATE)
if [ -z "$PASSCODE_VALUE" ] && [ "$OPEN_GATE_VALUE" != "1" ]; then
  echo "Cannot deploy: SHIZZLE_PASSCODE is empty or missing in .env — the passcode gate would be silently open (invariant E6). Set SHIZZLE_PASSCODE, or SHIZZLE_ALLOW_OPEN_GATE=1 for a deliberate open deployment." >&2
  exit 1
fi

PREV_IMAGE=$(sed -n 's/^SHIZZLE_API_IMAGE=//p' .env | tail -n 1)
PREV_TAG=$(sed -n 's/^SHIZZLE_API_TAG=//p' .env | tail -n 1)
if [ -z "$PREV_IMAGE" ] && [ -z "$PREV_TAG" ]; then
  echo "Refusing automated first deployment: the active release has no recorded API identity" >&2
  exit 1
fi

PREV_DB_REV=$(
  # Variables expand inside the postgres container, not in this shell.
  # shellcheck disable=SC2016
  "$DOCKER_BIN" compose -p shizzle -f compose.prod.yml exec -T postgres sh -c \
    'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT version_num FROM alembic_version"'
)
if ! [[ "$PREV_DB_REV" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "Cannot capture the previous database revision: $PREV_DB_REV" >&2
  exit 1
fi

ROLLBACK_ARCHIVE=.rollback-release.tar.gz
ROLLBACK_TARGET=.rollback-release-target
ROLLBACK_DB_REV=.rollback-db-revision
DEPLOY_PHASE=.deploy-phase
STAGE_DIR=$(mktemp -d "$PROD_DIR/.release-stage.XXXXXX")
trap 'rm -rf -- "$STAGE_DIR"; rm -f -- "$ROLLBACK_ARCHIVE.tmp" "$ROLLBACK_TARGET.tmp" "$ROLLBACK_DB_REV.tmp" "$DEPLOY_PHASE.tmp"' EXIT

tar -czf "$ROLLBACK_ARCHIVE.tmp" -- .env compose.prod.yml Caddyfile player
printf '%s\n' "$TARGET_SHA" > "$ROLLBACK_TARGET.tmp"
printf '%s\n' "$PREV_DB_REV" > "$ROLLBACK_DB_REV.tmp"
printf '%s\n' snapshot-ready > "$DEPLOY_PHASE.tmp"
mv -f "$ROLLBACK_ARCHIVE.tmp" "$ROLLBACK_ARCHIVE"
mv -f "$ROLLBACK_DB_REV.tmp" "$ROLLBACK_DB_REV"
mv -f "$ROLLBACK_TARGET.tmp" "$ROLLBACK_TARGET"
mv -f "$DEPLOY_PHASE.tmp" "$DEPLOY_PHASE"

"$DOCKER_BIN" pull "$IMAGE_REF"
tar -xzf incoming/shizzle-player.tar.gz -C "$STAGE_DIR"
test -d "$STAGE_DIR/dist"
install -m 600 .env "$STAGE_DIR/.env"
sed -i '/^SHIZZLE_API_TAG=/d; /^SHIZZLE_API_IMAGE=/d' "$STAGE_DIR/.env"
printf 'SHIZZLE_API_IMAGE=%s\n' "$IMAGE_REF" >> "$STAGE_DIR/.env"
install -m 644 incoming/compose.prod.yml "$STAGE_DIR/compose.prod.yml"
install -m 644 incoming/Caddyfile.prod "$STAGE_DIR/Caddyfile"
mv "$STAGE_DIR/dist" "$STAGE_DIR/player"

SHIZZLE_API_IMAGE=$IMAGE_REF "$DOCKER_BIN" compose \
  --env-file "$STAGE_DIR/.env" -p shizzle -f "$STAGE_DIR/compose.prod.yml" config -q
printf '%s\n' files-activating > "$DEPLOY_PHASE.tmp"
mv -f "$DEPLOY_PHASE.tmp" "$DEPLOY_PHASE"
mv -f "$STAGE_DIR/.env" .env
mv -f "$STAGE_DIR/compose.prod.yml" compose.prod.yml
mv -f "$STAGE_DIR/Caddyfile" Caddyfile
rm -rf -- player.next
mv "$STAGE_DIR/player" player.next
rm -rf -- player
mv player.next player

printf '%s\n' migration-started > "$DEPLOY_PHASE.tmp"
mv -f "$DEPLOY_PHASE.tmp" "$DEPLOY_PHASE"
"$DOCKER_BIN" compose -p shizzle -f compose.prod.yml run --rm --no-deps api alembic upgrade head
printf '%s\n' services-starting > "$DEPLOY_PHASE.tmp"
mv -f "$DEPLOY_PHASE.tmp" "$DEPLOY_PHASE"
"$DOCKER_BIN" compose -p shizzle -f compose.prod.yml up -d \
  --force-recreate --remove-orphans --no-deps api orchestrator caddy
echo "Activated $IMAGE_REF from database revision $PREV_DB_REV"
