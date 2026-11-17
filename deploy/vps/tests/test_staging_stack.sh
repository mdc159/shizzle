#!/usr/bin/env bash
# Staging stack and admin-listener contracts.
#
# Part 1 (always): structural assertions on compose.staging.yml, Caddyfile.staging,
# Caddyfile.prod, and the env templates.
# Part 2 (always): deploy-staging.sh transaction behavior with stubbed docker
# and curl — including the production-target refusals and rollback paths.
# Part 3 (only where a Docker daemon answers): compose config validation
# (published ports + resource limits), `caddy validate` of both real
# Caddyfiles through the caddy image, and live behavior of the admin routing
# (public 404 + header strip vs injected header on the loopback listeners).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
DEPLOY="$ROOT/deploy/vps/deploy-staging.sh"
CADDY_PROD="$ROOT/deploy/vps/Caddyfile.prod"
CADDY_STAGING="$ROOT/deploy/vps/Caddyfile.staging"
COMPOSE_PROD="$ROOT/deploy/vps/compose.prod.yml"
COMPOSE_STAGING="$ROOT/deploy/vps/compose.staging.yml"
ENV_EXAMPLE="$ROOT/.env.example"
ENV_STAGING_EXAMPLE="$ROOT/deploy/vps/.env.staging.example"

count() { grep -c "$@" || true; }

# --- Part 1: structural contracts ---------------------------------------------

# Public site still serves the same hostnames with the same TLS floor (also
# asserted by test_release_transaction.sh; kept here so this suite stands alone).
grep -Fq 'shizzle.systems, www.shizzle.systems {' "$CADDY_PROD"
grep -Fq 'protocols tls1.2 tls1.3' "$CADDY_PROD"

# Public site strips a forged admin header from EVERY request.
grep -Fq $'\theader >-X-Shizzle-Admin' "$CADDY_PROD"

# Admin denial handles exist, answer 404, and precede the /api/* proxy handle
# (handle blocks are mutually exclusive in file order).
admin_api_line=$(grep -n 'handle /api/admin/\* {' "$CADDY_PROD" | cut -d: -f1)
admin_page_line=$(grep -n 'handle /admin {' "$CADDY_PROD" | cut -d: -f1)
admin_tree_line=$(grep -n 'handle /admin/\* {' "$CADDY_PROD" | cut -d: -f1)
api_proxy_line=$(grep -n $'\thandle /api/\* {' "$CADDY_PROD" | head -1 | cut -d: -f1)
for line in "$admin_api_line" "$admin_page_line" "$admin_tree_line"; do
  [ -n "$line" ] || { echo "missing an admin denial handle in Caddyfile.prod" >&2; exit 1; }
  [ "$line" -lt "$api_proxy_line" ] || { echo "admin denial handle not before /api/* proxy" >&2; exit 1; }
done
[ "$(count -F 'respond 404' "$CADDY_PROD")" -ge 3 ]

# Tailnet-only admin listener exists and injects the secret header upstream.
grep -Fq ':18081 {' "$CADDY_PROD"
[ "$(count -F 'header_up X-Shizzle-Admin {$SHIZZLE_ADMIN_PROXY_SECRET:}' "$CADDY_PROD")" -ge 2 ]

# Production compose publishes the admin listener only on host loopback and
# keeps the public ports; api and caddy see the admin secret.
grep -Fq '"127.0.0.1:18081:18081"' "$COMPOSE_PROD"
grep -Fq '"80:80"' "$COMPOSE_PROD"
grep -Fq '"443:443"' "$COMPOSE_PROD"
[ "$(count -F 'SHIZZLE_ADMIN_PROXY_SECRET: ${SHIZZLE_ADMIN_PROXY_SECRET:-}' "$COMPOSE_PROD")" -ge 2 ]

# Staging compose: isolated project name, only 127.0.0.1:18080 published
# (a published-port string always contains a colon; expose-only entries do not).
grep -Fxq 'name: shizzle-staging' "$COMPOSE_STAGING"
staging_ports=$(grep -E '^\s+- "[^"]*:[^"]*"' "$COMPOSE_STAGING" || true)
[ "$staging_ports" = "$(printf '      - "127.0.0.1:18080:80"')" ] || {
  echo "staging compose publishes unexpected ports: $staging_ports" >&2
  exit 1
}
# CPU and memory limits plus restart policy on every staging service.
[ "$(count -F 'cpus:' "$COMPOSE_STAGING")" -ge 4 ]
[ "$(count -F 'memory:' "$COMPOSE_STAGING")" -ge 4 ]
[ "$(count -F 'restart: unless-stopped' "$COMPOSE_STAGING")" -ge 4 ]

# Staging Caddyfile: plain HTTP single listener, admin header injected, no /cdn
# route (staging media is signed-URL CloudFront; comments may mention /cdn).
grep -Fq ':80 {' "$CADDY_STAGING"
[ "$(count -F 'header_up X-Shizzle-Admin {$SHIZZLE_ADMIN_PROXY_SECRET:}' "$CADDY_STAGING")" -ge 2 ]
if grep -Eq 'handle(_path)? /cdn' "$CADDY_STAGING"; then
  echo "staging Caddyfile must not route /cdn (staging media is signed-URL CloudFront)" >&2
  exit 1
fi

# Env templates list the admin secret with no value, and staging points at a
# separate media bucket with no node tokens (it must not steal production nodes).
grep -Fq 'SHIZZLE_ADMIN_PROXY_SECRET=' "$ENV_EXAMPLE"
grep -Fq 'SHIZZLE_ADMIN_PROXY_SECRET=' "$ENV_STAGING_EXAMPLE"
grep -Fq 'S3_MEDIA_BUCKET=' "$ENV_STAGING_EXAMPLE"
if grep -q 'karaoke-pimpshizzle' "$ENV_STAGING_EXAMPLE"; then
  echo "staging env template must not name the production media bucket" >&2
  exit 1
fi
grep -Fq 'SHIZZLE_NODE_TOKENS=' "$ENV_STAGING_EXAMPLE"
if grep -Eq '^SHIZZLE_NODE_TOKENS=..*$' "$ENV_STAGING_EXAMPLE"; then
  echo "staging env template must keep SHIZZLE_NODE_TOKENS empty" >&2
  exit 1
fi
printf 'structural staging contracts passed\n'

# --- Part 2: deploy-staging.sh transaction with stubbed docker/curl ----------

TMP=$(mktemp -d)
BIN="$TMP/bin"
mkdir -p "$BIN"
trap 'rm -rf -- "$TMP"' EXIT

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
  *" config -q "*) [ "${DOCKER_FAIL:-}" != config ] || exit 30; exit 0 ;;
  *" up -d --wait postgres "*) [ "${DOCKER_FAIL:-}" != up ] || exit 34; exit 0 ;;
  *" alembic upgrade head "*) [ "${DOCKER_FAIL:-}" != migration ] || exit 32; exit 0 ;;
  *" up -d --force-recreate --remove-orphans --no-deps api orchestrator caddy "*)
    [ "${DOCKER_FAIL:-}" != up ] || exit 34; exit 0 ;;
  *" stop api orchestrator caddy "*) exit 0 ;;
  *" up -d --force-recreate --no-deps api orchestrator caddy "*) exit 0 ;;
esac
exit 0
STUB
chmod +x "$BIN/docker"

cat > "$BIN/curl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
if [ "${CURL_MODE:-ok}" = bad ]; then
  echo '{"status":"degraded","db":true,"orchestratorAlive":false}' >&2
  exit 7
fi
printf '%s' '{"status":"ok","db":true,"orchestratorAlive":true}'
STUB
chmod +x "$BIN/curl"

TARGET=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
DIGEST=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
IMAGE="ghcr.io/mdc159/shizzle/api:sha-$TARGET@sha256:$DIGEST"

new_fixture() {
  STG="$TMP/staging-$1"
  GUARD="$TMP/prodguard-$1"
  mkdir -p "$STG/player" "$STG/incoming/dist" "$GUARD"
  printf 'POSTGRES_USER=shizzle\nPOSTGRES_PASSWORD=x\nSHIZZLE_API_IMAGE=prev-image\n' > "$STG/.env"
  printf 'old-compose\n' > "$STG/compose.staging.yml"
  printf 'old-caddy\n' > "$STG/Caddyfile"
  printf 'old-player\n' > "$STG/player/index.html"
  printf 'new-compose\n' > "$STG/incoming/compose.staging.yml"
  printf 'new-caddy\n' > "$STG/incoming/Caddyfile.staging"
  printf 'new-player\n' > "$STG/incoming/dist/index.html"
  tar -C "$STG/incoming" -czf "$STG/incoming/shizzle-player.tar.gz" dist
  DOCKER_LOG="$STG/docker.log"
  : > "$DOCKER_LOG"
  PROD_GUARD="$GUARD"
  export PROD_GUARD DOCKER_LOG STG
}

run_deploy() {
  PATH="$BIN:$PATH" \
  SHIZZLE_STAGING_DIR="$STG" \
  SHIZZLE_STAGING_PROD_DIR="$PROD_GUARD" \
  SHIZZLE_DOCKER_BIN=docker \
  SHIZZLE_CURL_BIN=curl \
  SHIZZLE_STAGING_HEALTH_ATTEMPTS=2 \
  SHIZZLE_STAGING_HEALTH_SLEEP=0 \
  DOCKER_LOG="$DOCKER_LOG" DOCKER_FAIL="${DOCKER_FAIL:-}" CURL_MODE="${CURL_MODE:-ok}" \
    bash "$DEPLOY" "$IMAGE" "$TARGET"
}

# Production-target refusals (fail before anything is touched).
new_fixture refuse-prod-dir
STG="$PROD_GUARD" # aim the deploy at the production directory itself
if run_deploy 2> "$TMP/refuse1.err"; then
  echo "deploy-staging accepted the production directory" >&2; exit 1
fi
grep -Fq 'Refusing to operate on the production directory' "$TMP/refuse1.err"
test ! -e "$GUARD/.env"

new_fixture refuse-prod-subdir
STG="$PROD_GUARD/sub"
mkdir -p "$STG"
if run_deploy 2> "$TMP/refuse2.err"; then
  echo "deploy-staging accepted a directory inside production" >&2; exit 1
fi
grep -Fq 'Refusing to operate on the production directory' "$TMP/refuse2.err"

new_fixture refuse-root
STG=/
if run_deploy 2> "$TMP/refuse3.err"; then
  echo "deploy-staging accepted the root directory" >&2; exit 1
fi
grep -Fq 'Refusing unsafe staging directory: /' "$TMP/refuse3.err"

new_fixture refuse-prod-project
if SHIZZLE_STAGING_PROJECT=shizzle run_deploy 2> "$TMP/refuse4.err"; then
  echo "deploy-staging accepted the production project name" >&2; exit 1
fi
grep -Fq 'Refusing production Compose project name' "$TMP/refuse4.err"

new_fixture bad-args
if PATH="$BIN:$PATH" SHIZZLE_STAGING_DIR="$STG" SHIZZLE_STAGING_PROD_DIR="$PROD_GUARD" \
  bash "$DEPLOY" "some-other-image" "$TARGET" 2> "$TMP/refuse5.err"; then
  echo "deploy-staging accepted a non-pinned image reference" >&2; exit 1
fi
grep -Fq 'must pin the target SHA tag and a sha256 digest' "$TMP/refuse5.err"

new_fixture missing-files
rm "$STG/incoming/Caddyfile.staging"
if run_deploy 2> "$TMP/missing.err"; then
  echo "deploy-staging ran with a missing release asset" >&2; exit 1
fi
grep -Fq 'Cannot deploy: missing incoming/Caddyfile.staging' "$TMP/missing.err"

# Successful transaction.
new_fixture success
run_deploy
grep -Fx "SHIZZLE_API_IMAGE=$IMAGE" "$STG/.env"
grep -Fx new-compose "$STG/compose.staging.yml"
grep -Fx new-caddy "$STG/Caddyfile"
grep -Fx new-player "$STG/player/index.html"
grep -Fq "pull $IMAGE" "$DOCKER_LOG"
grep -Fq 'config -q' "$DOCKER_LOG"
grep -Fq 'up -d --wait postgres' "$DOCKER_LOG"
grep -Fq 'alembic upgrade head' "$DOCKER_LOG"
grep -Fq 'up -d --force-recreate --remove-orphans --no-deps api orchestrator caddy' "$DOCKER_LOG"
# Every compose invocation must target the staging project, never production.
if grep '^compose ' "$DOCKER_LOG" | grep -v ' -p shizzle-staging ' | grep -q .; then
  echo "deploy-staging issued a compose command outside the staging project" >&2; exit 1
fi
test ! -e "$STG/.staging-rollback.tar.gz"

# Config validation failure: nothing is activated, prior files survive.
new_fixture config-failure
DOCKER_FAIL=config
if run_deploy 2> "$TMP/config.err"; then
  echo "deploy-staging survived a compose config failure" >&2; exit 1
fi
DOCKER_FAIL=
grep -Fx old-compose "$STG/compose.staging.yml"
grep -Fx old-player "$STG/player/index.html"
test ! -e "$STG/.staging-rollback.tar.gz"

# Migration failure: prior files restored, previous image restarted.
new_fixture migration-failure
DOCKER_FAIL=migration
if run_deploy 2> "$TMP/migration.err"; then
  echo "deploy-staging survived a migration failure" >&2; exit 1
fi
DOCKER_FAIL=
grep -Fx old-compose "$STG/compose.staging.yml"
grep -Fx old-caddy "$STG/Caddyfile"
grep -Fx old-player "$STG/player/index.html"
grep -Fq 'up -d --force-recreate --no-deps api orchestrator caddy' "$DOCKER_LOG"
test ! -e "$STG/.staging-rollback.tar.gz"

# Health-check failure: prior files and image identity restored.
new_fixture health-failure
CURL_MODE=bad
if run_deploy 2> "$TMP/health.err"; then
  echo "deploy-staging survived a failing health check" >&2; exit 1
fi
CURL_MODE=ok
grep -Fx 'SHIZZLE_API_IMAGE=prev-image' "$STG/.env"
grep -Fx old-compose "$STG/compose.staging.yml"
grep -Fx old-player "$STG/player/index.html"
tail -n 1 "$DOCKER_LOG" | grep -Fq 'up -d --force-recreate --no-deps api orchestrator caddy'
test ! -e "$STG/.staging-rollback.tar.gz"

# First-deploy (bootstrap) failure: no previous image, application stopped.
new_fixture bootstrap-failure
sed -i '/^SHIZZLE_API_IMAGE=/d' "$STG/.env"
CURL_MODE=bad
if run_deploy 2> "$TMP/bootstrap.err"; then
  echo "bootstrap deploy survived a failing health check" >&2; exit 1
fi
CURL_MODE=ok
grep -Fq 'stop api orchestrator caddy' "$DOCKER_LOG"
test ! -e "$STG/.staging-rollback.tar.gz"
printf 'staging deploy transaction scenarios passed\n'

# --- Part 3: Docker-gated validation and live behavior -----------------------

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "NOTE: Docker is not available here; NOT RUN: compose config validation,"
  echo "caddy validate through the caddy image, and the live admin-routing checks."
  exit 0
fi

# Compose config of the staging stack validates and publishes only
# 127.0.0.1:18080, with CPU/memory limits on every service.
CFG=$(mktemp -d)
cp "$COMPOSE_STAGING" "$CFG/"
cat > "$CFG/.env" <<EOF
POSTGRES_PASSWORD=test-password
SHIZZLE_API_IMAGE=$IMAGE
SHIZZLE_ADMIN_PROXY_SECRET=test-admin-secret
EOF
(
  cd "$CFG"
  docker compose -p shizzle-staging --env-file ./.env -f compose.staging.yml config -q
  docker compose -p shizzle-staging --env-file ./.env -f compose.staging.yml config --format json \
    > "$CFG/staging-config.json"
)
python3 - "$CFG/staging-config.json" <<'PY'
import json, sys

cfg = json.load(open(sys.argv[1]))
assert cfg["name"] == "shizzle-staging", cfg["name"]
published = []
for name, svc in cfg["services"].items():
    for port in svc.get("ports", []):
        published.append(
            (name, port.get("host_ip"), str(port.get("published")), str(port.get("target")))
        )
    limits = ((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}
    assert limits.get("cpus") and limits.get("memory"), f"{name} lacks resource limits"
    if name in ("api", "caddy"):
        env = svc.get("environment") or {}
        assert env.get("SHIZZLE_ADMIN_PROXY_SECRET") == "test-admin-secret", name
assert published == [("caddy", "127.0.0.1", "18080", "80")], published
print("staging compose config: only 127.0.0.1:18080 published, limits on every service")
PY
rm -rf "$CFG"

# Production compose still validates without the new admin secret set
# (backward-compatible deploy until the operator fills it in).
CFG2=$(mktemp -d)
cp "$COMPOSE_PROD" "$CFG2/"
cat > "$CFG2/.env" <<EOF
POSTGRES_PASSWORD=test-password
SHIZZLE_API_IMAGE=$IMAGE
EOF
(
  cd "$CFG2"
  docker compose -p shizzle --env-file ./.env -f compose.prod.yml config -q
)
rm -rf "$CFG2"

# caddy validate of both real Caddyfiles through the caddy image.
docker run --rm -e SHIZZLE_ADMIN_PROXY_SECRET=validate-secret \
  -v "$CADDY_PROD":/etc/caddy/Caddyfile:ro caddy:2 \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker run --rm \
  -v "$CADDY_STAGING":/etc/caddy/Caddyfile:ro caddy:2 \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

# Live admin-routing behavior. The api upstream is a stub caddy that answers
# 209 only when X-Shizzle-Admin equals the secret, 200 otherwise, so status
# codes prove stripping/injection without trusting bodies. Site addresses are
# the only edits: public hostnames become a plain :18082 listener (TLS/ACME is
# untestable here) and the tls block is dropped with it; loopback listener
# addresses stay as authored.
NET=shizzle-staging-caddytest
docker network create "$NET"
STUB_CADDY="$TMP/stub-api.Caddyfile"
cat > "$STUB_CADDY" <<'EOF'
:8001 {
	@admin header X-Shizzle-Admin test-admin-secret
	respond @admin 209
	respond 200
}
EOF
BEHAVIOR_PROD="$TMP/Caddyfile.prod.behavior"
sed -e 's/^shizzle\.systems, www\.shizzle\.systems {/:18082 {/' \
  -e $'/^\ttls {$/,/^\t}$/d' "$CADDY_PROD" > "$BEHAVIOR_PROD"
cleanup() {
  docker rm -f stub-api caddy-prod-behavior caddy-staging-behavior >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap 'cleanup; rm -rf -- "$TMP"' EXIT

docker run -d --name stub-api --network "$NET" --network-alias api \
  -v "$STUB_CADDY":/etc/caddy/Caddyfile:ro caddy:2 >/dev/null
docker run -d --name caddy-prod-behavior --network "$NET" \
  -p 127.0.0.1:18081:18081 -p 127.0.0.1:18082:18082 \
  -e SHIZZLE_ADMIN_PROXY_SECRET=test-admin-secret \
  -v "$BEHAVIOR_PROD":/etc/caddy/Caddyfile:ro caddy:2 >/dev/null
docker run -d --name caddy-staging-behavior --network "$NET" \
  -p 127.0.0.1:18080:80 \
  -e SHIZZLE_ADMIN_PROXY_SECRET=test-admin-secret \
  -v "$CADDY_STAGING":/etc/caddy/Caddyfile:ro caddy:2 >/dev/null

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
for attempt in $(seq 1 30); do
  [ "$(code http://127.0.0.1:18082/api/health)" = 200 ] && break
  sleep 1
done

# Public site: admin paths 404 at the edge; a forged header never reaches api.
[ "$(code http://127.0.0.1:18082/api/admin/x)" = 404 ]
[ "$(code http://127.0.0.1:18082/admin)" = 404 ]
[ "$(code http://127.0.0.1:18082/admin/sub/page)" = 404 ]
[ "$(code -H 'X-Shizzle-Admin: forged-value' http://127.0.0.1:18082/api/health)" = 200 ]
[ "$(code http://127.0.0.1:18082/api/health)" = 200 ]
# Tailnet-only admin listener: correct secret injected, admin routes proxied.
[ "$(code http://127.0.0.1:18081/api/health)" = 209 ]
[ "$(code http://127.0.0.1:18081/api/admin/x)" = 209 ]
[ "$(code -H 'X-Shizzle-Admin: forged-value' http://127.0.0.1:18081/api/admin/x)" = 209 ]
# Staging listener: same injection, so /admin works on staging.
[ "$(code http://127.0.0.1:18080/api/health)" = 209 ]
[ "$(code http://127.0.0.1:18080/api/admin/x)" = 209 ]

printf 'docker-gated staging and admin-routing checks passed\n'
