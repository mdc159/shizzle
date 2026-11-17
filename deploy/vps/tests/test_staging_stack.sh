#!/usr/bin/env bash
# Staging stack and admin-listener contracts.
#
# Part 1 (always): structural assertions on compose.staging.yml, Caddyfile.staging,
# Caddyfile.prod, and the env templates.
# Part 2 (always): deploy-staging.sh transaction behavior with stubbed docker
# and curl — production-target refusals (including the non-overridable guard),
# fresh-directory bootstrap success/failure, and rollback ordering.
# Part 3 (only where a Docker daemon answers): compose config validation
# (published ports + resource limits), `caddy validate` of both real
# Caddyfiles through the caddy image, and live behavior of the admin routing:
# the public site strips X-Shizzle-Admin before /api, /ws AND the /cdn proxy
# and 404s admin paths, while the loopback listeners inject exactly the
# secret — and never on the /cdn upstream.
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

# Parse route/block boundaries rather than relying on nearby context lines.
# Every admin-header injection must be inside an /api/* or /ws/* handle; in
# particular /cdn/* must never receive the secret, even after extra lines or
# nested reverse_proxy blocks are added.
assert_admin_header_routes() {
  awk '
    FNR == 1 { depth = 0; route = ""; route_depth = -1 }
    /^[[:space:]]*handle(_path)?[[:space:]]+/ {
      route = $2
      route_depth = depth
    }
    /header_up[[:space:]]+X-Shizzle-Admin([[:space:]]|$)/ {
      if (route != "/api/*" && route != "/ws/*") {
        printf "%s:%d: admin header injected outside /api/* and /ws/* (route %s)\n", FILENAME, FNR, route > "/dev/stderr"
        bad = 1
      }
    }
    {
      depth += gsub(/{/, "{")
      depth -= gsub(/}/, "}")
      if (route != "" && depth <= route_depth) route = ""
    }
    END { exit bad ? 1 : 0 }
  ' "$@"
}

assert_no_public_wildcard_ports() {
  if grep -Eq '^[[:space:]]*-[[:space:]]*"(0\.0\.0\.0:|\[::\]:)?(80:80|443:443(/udp)?)"' "$1"; then
    echo "production Caddy publishes a public port on every address" >&2
    return 1
  fi
}

WORKFLOW_STAGING="$ROOT/.github/workflows/deploy-staging.yml"

# --- Part 1: structural contracts ---------------------------------------------

# Public site still serves the same hostnames with the same TLS floor (also
# asserted by test_release_transaction.sh; kept here so this suite stands alone).
grep -Fq 'shizzle.systems, www.shizzle.systems {' "$CADDY_PROD"
grep -Fq 'protocols tls1.2 tls1.3' "$CADDY_PROD"

# Public site strips a forged admin header from EVERY request, including /ws
# and /cdn: request_header (not the response-side `header >-...`) at site
# scope, before any routing.
grep -Fq $'\trequest_header -X-Shizzle-Admin' "$CADDY_PROD"
if grep -Fq 'header >-X-Shizzle-Admin' "$CADDY_PROD"; then
  echo "Caddyfile.prod must strip the request header, not edit responses" >&2
  exit 1
fi

# Admin denial handles exist, answer 404, and precede the /api/* proxy handle
# (handle blocks are mutually exclusive in file order).
admin_api_exact_line=$(grep -n 'handle /api/admin {' "$CADDY_PROD" | cut -d: -f1)
admin_api_line=$(grep -n 'handle /api/admin/\* {' "$CADDY_PROD" | cut -d: -f1)
admin_page_line=$(grep -n 'handle /admin {' "$CADDY_PROD" | cut -d: -f1)
admin_tree_line=$(grep -n 'handle /admin/\* {' "$CADDY_PROD" | cut -d: -f1)
api_proxy_line=$(grep -n $'\thandle /api/\* {' "$CADDY_PROD" | head -1 | cut -d: -f1)
for line in "$admin_api_exact_line" "$admin_api_line" "$admin_page_line" "$admin_tree_line"; do
  [ -n "$line" ] || { echo "missing an admin denial handle in Caddyfile.prod" >&2; exit 1; }
  [ "$line" -lt "$api_proxy_line" ] || { echo "admin denial handle not before /api/* proxy" >&2; exit 1; }
done
[ "$(count -F 'respond 404' "$CADDY_PROD")" -ge 4 ]

# Tailnet-only admin listener exists. Injection is limited to the api and ws
# upstreams (exactly two per file) so the secret never travels to CloudFront.
grep -Fq ':18081 {' "$CADDY_PROD"
[ "$(count -F 'header_up X-Shizzle-Admin {$SHIZZLE_ADMIN_PROXY_SECRET:}' "$CADDY_PROD")" = 2 ]
[ "$(count -F 'header_up X-Shizzle-Admin {$SHIZZLE_ADMIN_PROXY_SECRET:}' "$CADDY_STAGING")" = 2 ]
assert_admin_header_routes "$CADDY_PROD" "$CADDY_STAGING"

# Production compose publishes the admin listener only on host loopback and
# keeps the public ports; api and caddy see the admin secret.
grep -Fq '"127.0.0.1:18081:18081"' "$COMPOSE_PROD"
# Public ports bind the public address only, never every address: Tailscale
# Serve owns tailnet:443 and a 0.0.0.0 bind fails after a reboot (2026-11-17).
grep -Fq '"${SHIZZLE_PUBLIC_IP:-72.60.173.171}:80:80"' "$COMPOSE_PROD"
grep -Fq '"${SHIZZLE_PUBLIC_IP:-72.60.173.171}:443:443"' "$COMPOSE_PROD"
assert_no_public_wildcard_ports "$COMPOSE_PROD"
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
# Required staging credentials must start empty, not as publicly known valid
# values. Compose's :? guards reject a copied, unedited template.
if grep -Eq '^(SHIZZLE_PASSCODE|TOKEN_SIGNING_SECRET|POSTGRES_PASSWORD|DATABASE_URL)=.+' "$ENV_STAGING_EXAMPLE"; then
  echo "staging env template contains a non-empty credential placeholder" >&2
  exit 1
fi
grep -Fq 'SHIZZLE_PASSCODE: ${SHIZZLE_PASSCODE:?' "$COMPOSE_STAGING"
grep -Fq 'TOKEN_SIGNING_SECRET: ${TOKEN_SIGNING_SECRET:?' "$COMPOSE_STAGING"
[ "$(count -F 'DATABASE_URL: ${DATABASE_URL:?' "$COMPOSE_STAGING")" = 2 ]
if grep -E '^[[:space:]]+DATABASE_URL:.*POSTGRES_PASSWORD' "$COMPOSE_STAGING"; then
  echo "staging DATABASE_URL must use explicit URL-encoded credentials, not raw POSTGRES_PASSWORD" >&2
  exit 1
fi
if grep -Eq '^SHIZZLE_NODE_TOKENS=..*$' "$ENV_STAGING_EXAMPLE"; then
  echo "staging env template must keep SHIZZLE_NODE_TOKENS empty" >&2
  exit 1
fi

# Deploy-workflow trust split, as a regression assertion: the deploy job must
# check out the repository's default branch EXPLICITLY — with workflow_dispatch
# an omitted ref follows the ref the workflow was dispatched from (possibly a
# feature branch), which would let that ref supply the root-executed deploy
# assets again. The chosen ref is checked out only by the player build.
[ "$(count -F 'ref: ${{ github.event.repository.default_branch }}' "$WORKFLOW_STAGING")" = 1 ]
grep -Fq 'ref: ${{ needs.resolve.outputs.target_sha }}' "$WORKFLOW_STAGING"
grep -Fq 'name: staging' "$WORKFLOW_STAGING"
# The packages:write reusable workflow must come from a reviewed immutable
# commit, not the dispatched ref (the staging environment protects only the
# later deploy job).
grep -Fq 'uses: mdc159/shizzle/.github/workflows/api-image.yml@b6cf7129606bb0b903daa709ddf7a7dd3b409d46' "$WORKFLOW_STAGING"
if grep -Fq 'uses: ./.github/workflows/api-image.yml' "$WORKFLOW_STAGING"; then
  echo "privileged API image workflow resolves from the untrusted dispatched ref" >&2
  exit 1
fi
# N2: the deploy job guards its dispatch ref (convenience check) before it
# checks out the default branch; the environment rule is the real control.
grep -Fq 'DEFAULT_REF: refs/heads/${{ github.event.repository.default_branch }}' "$WORKFLOW_STAGING"
grep -Fq 'Dispatched from' "$WORKFLOW_STAGING"
guard_line=$(grep -n 'Guard dispatch ref' "$WORKFLOW_STAGING" | cut -d: -f1)
deploy_checkout_line=$(grep -n 'ref: ${{ github.event.repository.default_branch }}' "$WORKFLOW_STAGING" | cut -d: -f1)
[ -n "$guard_line" ] && [ -n "$deploy_checkout_line" ] \
  && [ "$guard_line" -lt "$deploy_checkout_line" ] \
  || { echo "deploy job's dispatch-ref guard missing or out of order" >&2; exit 1; }
# N1 (workflow side): the player build refuses non-regular output before the
# artifact is made; the deploy script never extracts preserving owners/modes.
grep -Fq 'Refuse non-regular player output' "$WORKFLOW_STAGING"
grep -Fq -- '--no-same-owner' "$DEPLOY"
grep -Fq -- '--no-same-permissions' "$DEPLOY"
# N3: config persistence is off in both Caddyfiles — the env-substituted
# admin secret must never land in /config/caddy/autosave.json (E3).
grep -Fq $'\tpersist_config off' "$CADDY_PROD"
grep -Fq $'\tpersist_config off' "$CADDY_STAGING"
printf 'structural staging contracts passed\n'

# --- Part 2: deploy-staging.sh transaction with stubbed docker/curl ----------

REAL_MKTEMP=$(command -v mktemp)
REAL_MV=$(command -v mv)
TMP=$(mktemp -d)
BIN="$TMP/bin"
mkdir -p "$BIN"
trap 'rm -rf -- "$TMP"' EXIT

# Python is used for crafted archives and the offline policy evaluation.
PY_BIN=""
for py_candidate in python3 python; do
  if "$py_candidate" -c 'import tarfile' >/dev/null 2>&1; then PY_BIN="$py_candidate"; break; fi
done

# Negative fixtures prove the structural security guards themselves reject
# regressions that the old two-line context check and unqualified-port regex
# missed. Keep the total admin-header count at two in the Caddy fixture: one
# injection is moved from /api/* into a distant line inside /cdn/*.
BAD_CADDY="$TMP/Caddyfile.bad-cdn-header"
awk '
  /header_up[[:space:]]+X-Shizzle-Admin/ && !removed { removed = 1; next }
  { print }
  /header_up Host d2488k8kjndpsy\.cloudfront\.net/ && !added {
    print ""
    print ""
    print "\t\t\theader_up X-Shizzle-Admin {$SHIZZLE_ADMIN_PROXY_SECRET:}"
    added = 1
  }
' "$CADDY_PROD" > "$BAD_CADDY"
[ "$(count -F 'header_up X-Shizzle-Admin {$SHIZZLE_ADMIN_PROXY_SECRET:}' "$BAD_CADDY")" = 2 ]
if assert_admin_header_routes "$BAD_CADDY" 2>/dev/null; then
  echo "block-aware admin-header guard accepted a /cdn injection" >&2
  exit 1
fi
for binding in '0.0.0.0:80:80' '[::]:443:443' '0.0.0.0:443:443/udp'; do
  BAD_PORTS="$TMP/compose.bad-ports"
  printf '      - "%s"\n' "$binding" > "$BAD_PORTS"
  if assert_no_public_wildcard_ports "$BAD_PORTS" 2>/dev/null; then
    echo "wildcard-port guard accepted $binding" >&2
    exit 1
  fi
done

# --- N5 follow-up: the worker policy's ListBucket grant must be usable by
# the first-attempt absent-handoff probe. That probe is a GetObject (no
# s3:prefix exists in its request context), so S3 can only return 404
# instead of 403 if s3:ListBucket on the bucket is allowed UNCONDITIONED.
# This is an offline evaluation of the documented policy — not live AWS
# acceptance.
WORKER_POLICY="$TMP/worker-policy.json"
awk '/^ *```json/{buf=""; f=1; next} /^ *```$/{if (f && buf ~ /ListForAbsentHandoffAndSourceChecks/) printf "%s", buf; f=0; next} f{buf = buf $0 "\n"}' \
  "$ROOT/deploy/vps/README.md" > "$WORKER_POLICY"
[ -s "$WORKER_POLICY" ] || { echo "could not extract the worker policy JSON from the README" >&2; exit 1; }
if grep -q 's3:prefix\|"Condition"' "$WORKER_POLICY"; then
  echo "worker policy ListBucket grant is conditioned; the absent-handoff probe has no s3:prefix context" >&2
  exit 1
fi
if [ -n "$PY_BIN" ]; then
  "$PY_BIN" - "$WORKER_POLICY" <<'PY'
import copy, json, sys

policy = json.load(open(sys.argv[1]))
stmts = policy["Statement"]
BUCKET = "arn:aws:s3:::shizzle-staging-media"

def evaluate(statements, action, resource, context):
    for st in statements:
        if action not in st.get("Action", []):
            continue
        res = st.get("Resource", [])
        res = [res] if isinstance(res, str) else res
        if resource not in res:
            continue
        matched = True
        for block in st.get("Condition", {}).values():
            for key in block:
                if key not in context:
                    matched = False  # absent condition key: statement cannot match
        if not matched:
            continue
        return st["Effect"] == "Allow"
    return False

# First-attempt probe context: a GetObject request carries no s3:prefix.
probe_ctx = {}
assert evaluate(stmts, "s3:ListBucket", BUCKET, probe_ctx), (
    "ListBucket ineffective without s3:prefix context: the absent-handoff "
    "probe would get 403 and the first attempt could not start"
)
# Regression: the rejected prefix-conditioned form must evaluate to Deny.
bad = copy.deepcopy(policy)
for st in bad["Statement"]:
    if "s3:ListBucket" in st.get("Action", []):
        st["Condition"] = {"StringLike": {"s3:prefix": ["sources/*"]}}
assert not evaluate(bad["Statement"], "s3:ListBucket", BUCKET, probe_ctx)
# Object grants stay prefix-scoped; no s3:HeadObject anywhere.
actions = {a for st in policy["Statement"] for a in st["Action"]}
assert "s3:HeadObject" not in actions
resources = [r for st in policy["Statement"]
             for r in ([st["Resource"]] if isinstance(st["Resource"], str) else st["Resource"])]
assert resources == [
    BUCKET,
    BUCKET + "/sources/*",
    BUCKET + "/tracks/*/1/separation/attempts/*",
], resources
print("worker policy offline evaluation: unconditional ListBucket covers the "
      "absent-handoff probe (offline check, not live AWS acceptance); "
      "object grants prefix-scoped")
PY
else
  echo "NOTE: no python available; offline policy evaluation NOT RUN (grep-level checks only)"
fi

cat > "$BIN/docker" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$DOCKER_LOG"
if [ "${1:-}" = pull ]; then
  [ "${DOCKER_FAIL:-}" != pull ] || exit 31
  exit 0
fi
if [ "${1:-}" != compose ]; then exit 0; fi
active_image() {
  # The deploy script runs docker from the staging directory, so the ./.env
  # here is the ACTIVE release's environment.
  sed -n 's/^SHIZZLE_API_IMAGE=//p' ./.env | tail -n 1
}
case " $* " in
  *" config -q "*) [ "${DOCKER_FAIL:-}" != config ] || exit 30; exit 0 ;;
  *" up -d --wait postgres "*) [ "${DOCKER_FAIL:-}" != up ] || exit 34; exit 0 ;;
  *" alembic upgrade head "*) [ "${DOCKER_FAIL:-}" != migration ] || exit 32; exit 0 ;;
  *" up -d --force-recreate --remove-orphans --no-deps api orchestrator caddy "*)
    [ "${DOCKER_FAIL:-}" != up ] || exit 34; exit 0 ;;
  *" stop api orchestrator caddy "*)
    # A rollback stop must run while the FAILED release's image identity is
    # still the active one (stop-before-restore, F4). A stop after restoring
    # the prior .env fails here instead.
    if [ -n "${DOCKER_EXPECT_STOP_IMAGE:-}" ]; then
      active=$(active_image)
      if [ "$active" != "$DOCKER_EXPECT_STOP_IMAGE" ]; then
        printf 'stop ran with the wrong active image: %s\n' "$active" >> "$DOCKER_LOG"
        exit 36
      fi
    fi
    [ "${DOCKER_FAIL:-}" != stop ] || exit 35
    exit 0
    ;;
  *" up -d --force-recreate --no-deps api orchestrator caddy "*)
    [ "${DOCKER_FAIL:-}" != restart ] || exit 38
    # The rollback restart must run only after the PREVIOUS release's
    # identity is active again.
    if [ -n "${DOCKER_EXPECT_RESTART_IMAGE:-}" ]; then
      active=$(active_image)
      if [ "$active" != "$DOCKER_EXPECT_RESTART_IMAGE" ]; then
        printf 'rollback restart ran with the wrong active image: %s\n' "$active" >> "$DOCKER_LOG"
        exit 37
      fi
    fi
    exit 0
    ;;
  *" ps -q orchestrator "*)
    printf '%s\n' "${DOCKER_ORCHESTRATOR_CONTAINER_ID:-cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc}"
    exit 0
    ;;
  *" exec -T postgres "*)
    # The query must be tied to THIS replacement container, not MAX(updated_at).
    case "$*" in
      *"worker_id LIKE 'cccccccccccc-%'"*) ;;
      *) printf 'heartbeat query did not target the replacement container\n' >> "$DOCKER_LOG"; exit 39 ;;
    esac
    if [ "${DOCKER_HEARTBEAT_MODE:-new}" = old-only ]; then
      printf 'f\n'
    else
      printf 't\n'
    fi
    exit 0
    ;;
esac
exit 0
STUB
chmod +x "$BIN/docker"

# Fault injection for rollback operations that run with errexit suppressed by
# bash's conditional-function semantics. Outside the selected failure each
# wrapper delegates to the real host tool.
cat > "$BIN/mktemp" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
if [ -n "${MKTEMP_FAIL_PATTERN:-}" ] && [[ "$*" == *"$MKTEMP_FAIL_PATTERN"* ]]; then
  exit 70
fi
exec "$REAL_MKTEMP" "$@"
STUB
cat > "$BIN/mv" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
if [ "${MV_FAIL:-}" = restore-env ]; then
  case "$*" in
    *".staging-restore."*"/.env .env") exit 71 ;;
  esac
fi
if [ "${MV_FAIL:-}" = activate-caddy ]; then
  case "$*" in
    *".staging-stage."*"/Caddyfile Caddyfile") exit 72 ;;
  esac
fi
exec "$REAL_MV" "$@"
STUB
chmod +x "$BIN/mktemp" "$BIN/mv"

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

# A staging directory with a previous release on it.
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

# A fresh staging directory: only .env (no image identity) and incoming/.
new_fresh_fixture() {
  STG="$TMP/fresh-$1"
  GUARD="$TMP/freshguard-$1"
  mkdir -p "$STG/incoming/dist" "$GUARD"
  printf 'POSTGRES_USER=shizzle\nPOSTGRES_PASSWORD=x\n' > "$STG/.env"
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
  REAL_MKTEMP="$REAL_MKTEMP" REAL_MV="$REAL_MV" \
  MKTEMP_FAIL_PATTERN="${MKTEMP_FAIL_PATTERN:-}" MV_FAIL="${MV_FAIL:-}" \
  DOCKER_HEARTBEAT_MODE="${DOCKER_HEARTBEAT_MODE:-new}" \
  DOCKER_EXPECT_STOP_IMAGE="${DOCKER_EXPECT_STOP_IMAGE:-}" \
  DOCKER_EXPECT_RESTART_IMAGE="${DOCKER_EXPECT_RESTART_IMAGE:-}" \
    bash "$DEPLOY" "$IMAGE" "$TARGET"
}

# Production-target refusals (fail before anything is touched).
new_fixture refuse-prod-dir
STG="$PROD_GUARD" # aim the deploy at the additional guard directory
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

# The canonical production path is always refused, whatever the caller put in
# the environment: the extra guard cannot remove the built-in denylist.
new_fixture refuse-canonical-prod
PROD_GUARD="$TMP/nowhere-special"
export PROD_GUARD
for canonical in /opt/shizzle/prod /opt/shizzle/prod/sub; do
  if PATH="$BIN:$PATH" SHIZZLE_STAGING_DIR="$canonical" SHIZZLE_STAGING_PROD_DIR="$PROD_GUARD" \
    SHIZZLE_DOCKER_BIN=docker SHIZZLE_CURL_BIN=curl \
    bash "$DEPLOY" "$IMAGE" "$TARGET" 2> "$TMP/refuse-canonical.err"; then
    echo "deploy-staging accepted the canonical production path $canonical" >&2; exit 1
  fi
  grep -Fq "Refusing to operate on the production directory: $canonical" "$TMP/refuse-canonical.err"
done

# An unresolvable additional guard fails closed (symlink loop; only provable
# where symlinks really exist, i.e. Linux CI).
ln -s "$TMP/guard-loop-b" "$TMP/guard-loop-a" 2>/dev/null || true
ln -s "$TMP/guard-loop-a" "$TMP/guard-loop-b" 2>/dev/null || true
if [ -L "$TMP/guard-loop-a" ] && [ -L "$TMP/guard-loop-b" ]; then
  new_fixture refuse-unresolvable-guard
  if SHIZZLE_STAGING_PROD_DIR="$TMP/guard-loop-a" SHIZZLE_STAGING_DIR="$STG" \
    PATH="$BIN:$PATH" SHIZZLE_DOCKER_BIN=docker SHIZZLE_CURL_BIN=curl \
    bash "$DEPLOY" "$IMAGE" "$TARGET" 2> "$TMP/refuse-loop.err"; then
    echo "deploy-staging accepted an unresolvable guard path" >&2; exit 1
  fi
  grep -Fq 'failing closed' "$TMP/refuse-loop.err"
else
  echo "NOTE: symlink loop not constructible on this filesystem; unresolvable-guard refusal NOT RUN"
fi

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

new_fixture missing-release-file
rm -rf "$STG/player"
if run_deploy 2> "$TMP/missing-release.err"; then
  echo "deploy-staging ran with a prior release half-missing" >&2; exit 1
fi
grep -Fq 'Cannot deploy: missing player' "$TMP/missing-release.err"

# Stale release files with no recorded image identity are refused, not guessed.
new_fresh_fixture refuse-stale-tree
printf 'old-compose\n' > "$STG/compose.staging.yml"
if run_deploy 2> "$TMP/stale.err"; then
  echo "deploy-staging accepted a stale release tree on a first deploy" >&2; exit 1
fi
grep -Fq 'Refusing first deployment with stale release file present: compose.staging.yml' "$TMP/stale.err"

# A crashed deploy's rollback snapshot is the only reliable prior-release
# copy. Refuse to overwrite it or to snapshot the partially activated tree.
new_fixture stale-rollback
printf 'only-prior-release-snapshot\n' > "$STG/.staging-rollback.tar.gz"
if run_deploy 2> "$TMP/stale-rollback.err"; then
  echo "deploy-staging overwrote a stale rollback archive" >&2; exit 1
fi
grep -Fq 'Refusing staging deploy while rollback artifact exists' "$TMP/stale-rollback.err"
grep -Fx 'only-prior-release-snapshot' "$STG/.staging-rollback.tar.gz"
grep -Fx old-compose "$STG/compose.staging.yml"
[ ! -s "$STG/docker.log" ] || { echo "docker ran despite a stale rollback archive" >&2; exit 1; }

# The incomplete temporary snapshot is also an unclosed transaction marker.
new_fixture stale-rollback-tmp
printf 'incomplete-snapshot\n' > "$STG/.staging-rollback.tar.gz.tmp"
if run_deploy 2> "$TMP/stale-rollback-tmp.err"; then
  echo "deploy-staging removed an incomplete prior rollback snapshot" >&2; exit 1
fi
grep -Fx 'incomplete-snapshot' "$STG/.staging-rollback.tar.gz.tmp"

# --- N1: crafted player archives must be refused with the staging directory
# left byte-identical. Archives are crafted with python3's tarfile so member
# types (symlink, hard link) and hostile names (absolute, ..) are exact on
# every filesystem — no symlink support required. A normal archive still
# succeeding is proven by the success and fresh-success cases below.
if [ -n "$PY_BIN" ]; then
  dir_digest() {
    (cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
  }
  craft_player_archive() {
    "$PY_BIN" - "$1" <<'PY'
import io, sys, tarfile

mode = sys.argv[1]

def add(tf, name, data=b"x", typ=tarfile.REGTYPE, link=""):
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    ti.type = typ
    ti.linkname = link
    tf.addfile(ti, io.BytesIO(data) if typ == tarfile.REGTYPE else None)

with tarfile.open("crafted-player.tar.gz", "w:gz") as tf:
    if mode == "dist-link":
        add(tf, "dist", typ=tarfile.SYMTYPE, link="/opt/shizzle/prod")
    elif mode == "link-inside":
        add(tf, "dist/", b"", tarfile.DIRTYPE)
        add(tf, "dist/index.html", b"<html>")
        add(tf, "dist/link", typ=tarfile.SYMTYPE, link="index.html")
    elif mode == "hardlink-inside":
        add(tf, "dist/", b"", tarfile.DIRTYPE)
        add(tf, "dist/a", b"A")
        add(tf, "dist/b", typ=tarfile.LNKTYPE, link="dist/a")
    elif mode == "absolute-member":
        add(tf, "dist/", b"", tarfile.DIRTYPE)
        add(tf, "dist/index.html", b"<html>")
        add(tf, "/etc/evil", b"evil")
    elif mode == "dotdot-member":
        add(tf, "dist/", b"", tarfile.DIRTYPE)
        add(tf, "dist/index.html", b"<html>")
        add(tf, "../evil", b"evil")
    elif mode == "dotdot-inside":
        add(tf, "dist/", b"", tarfile.DIRTYPE)
        add(tf, "dist/index.html", b"<html>")
        add(tf, "dist/../evil", b"evil")
    elif mode == "dotdot-deep":
        add(tf, "dist/", b"", tarfile.DIRTYPE)
        add(tf, "dist/a/", b"", tarfile.DIRTYPE)
        add(tf, "dist/a/../../evil", b"evil")
    else:
        raise SystemExit("unknown mode")
PY
  }
  refuse_crafted() {
    new_fixture "craft-$1"
    (cd "$STG/incoming" && craft_player_archive "$1" && mv -f crafted-player.tar.gz shizzle-player.tar.gz)
    before=$(dir_digest "$STG")
    if run_deploy 2> "$TMP/craft-$1.err"; then
      echo "crafted archive ($1) was accepted" >&2; exit 1
    fi
    grep -Fq "$2" "$TMP/craft-$1.err"
    after=$(dir_digest "$STG")
    [ "$before" = "$after" ] || { echo "staging directory changed after refusing $1" >&2; exit 1; }
    test ! -e "$STG/.staging-rollback.tar.gz"
    # Refusal happens before any pull or Compose command: the stubbed docker
    # must never have been invoked.
    [ ! -s "$STG/docker.log" ] || { echo "docker was invoked while refusing $1" >&2; exit 1; }
  }
  refuse_crafted dist-link "non-regular member types"
  refuse_crafted link-inside "non-regular member types"
  refuse_crafted hardlink-inside "non-regular member types"
  refuse_crafted absolute-member "absolute member paths"
  refuse_crafted dotdot-member "'..' path components"
  refuse_crafted dotdot-inside "'..' path components"
  refuse_crafted dotdot-deep "'..' path components"

  # A valid LARGE archive must still deploy: the listing consumers read the
  # whole stream, so a big regular archive cannot die of SIGPIPE (141 0) and
  # be falsely refused under pipefail.
  new_fixture large-archive
  (cd "$STG/incoming" && "$PY_BIN" - <<'PY'
import io, tarfile

with tarfile.open("shizzle-player.tar.gz", "w:gz") as tf:
    dist = tarfile.TarInfo("dist/")
    dist.type = tarfile.DIRTYPE
    dist.mode = 0o755  # realistic: what `tar -czf` stores for directories
    tf.addfile(dist)
    for i in range(2000):
        data = ("asset-%04d" % i).encode()
        ti = tarfile.TarInfo("dist/assets/f-%04d.js" % i)
        ti.size = len(data)
        ti.mode = 0o644
        tf.addfile(ti, io.BytesIO(data))
    html = b"<html>ok</html>"
    ti = tarfile.TarInfo("dist/index.html")
    ti.size = len(html)
    ti.mode = 0o644
    tf.addfile(ti, io.BytesIO(html))
PY
  )
  run_deploy
  grep -Fx "SHIZZLE_API_IMAGE=$IMAGE" "$STG/.env"
  grep -Fx "<html>ok</html>" "$STG/player/index.html"
  [ "$(find "$STG/player/assets" -type f | wc -l)" -eq 2000 ]
  test ! -e "$STG/.staging-rollback.tar.gz"
else
  echo "NOTE: python3 unavailable; crafted-archive refusals NOT RUN"
fi

# Successful transaction over an existing release.
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
grep -Fq 'ps -q orchestrator' "$DOCKER_LOG"
grep -Fq "worker_id LIKE 'cccccccccccc-%'" "$DOCKER_LOG"
# Every compose invocation must target the staging project, never production.
if grep '^compose ' "$DOCKER_LOG" | grep -v ' -p shizzle-staging ' | grep -q .; then
  echo "deploy-staging issued a compose command outside the staging project" >&2; exit 1
fi
test ! -e "$STG/.staging-rollback.tar.gz"

# Successful FIRST deployment into a fresh directory.
new_fresh_fixture fresh-success
run_deploy
grep -Fx "SHIZZLE_API_IMAGE=$IMAGE" "$STG/.env"
grep -Fx new-compose "$STG/compose.staging.yml"
grep -Fx new-caddy "$STG/Caddyfile"
grep -Fx new-player "$STG/player/index.html"
grep -Fq 'up -d --wait postgres' "$DOCKER_LOG"
grep -Fq 'alembic upgrade head' "$DOCKER_LOG"
grep -Fq 'up -d --force-recreate --remove-orphans --no-deps api orchestrator caddy' "$DOCKER_LOG"
test ! -e "$STG/.staging-rollback.tar.gz"

# Failed FIRST deployment: stopped while the failed config was active, then
# the directory returns to its fresh state (no image identity, no release files).
new_fresh_fixture fresh-failure
CURL_MODE=bad
DOCKER_EXPECT_STOP_IMAGE="$IMAGE"
set +e
run_deploy 2> "$TMP/fresh-fail.err"
fresh_status=$?
set -e
CURL_MODE=ok
DOCKER_EXPECT_STOP_IMAGE=
test "$fresh_status" -eq 1 || { echo "fresh deploy failed with unexpected status $fresh_status (expected 1)" >&2; exit 1; }
grep -Fq 'stop api orchestrator caddy' "$DOCKER_LOG"
grep -Fq 'rolling back' "$TMP/fresh-fail.err"
if grep -q 'wrong active image' "$DOCKER_LOG"; then
  echo "rollback stop ran against the wrong release identity" >&2; exit 1
fi
# .env back to no image identity; activated release files removed.
if sed -n 's/^SHIZZLE_API_IMAGE=//p' "$STG/.env" | grep -q .; then
  echo "fresh-deploy rollback left an image identity behind" >&2; exit 1
fi
test ! -e "$STG/compose.staging.yml"
test ! -e "$STG/Caddyfile"
test ! -e "$STG/player"
test ! -e "$STG/.staging-rollback.tar.gz"
# Stop happens while the failed release (new identity) is still active: the
# stop call precedes the file restoration, observable as stop being the last
# compose invocation of the failed transaction.
tail -n 1 "$DOCKER_LOG" | grep -Fq 'stop api orchestrator caddy'

# Config validation failure: nothing is activated (no stop), prior files survive.
new_fixture config-failure
DOCKER_FAIL=config
if run_deploy 2> "$TMP/config.err"; then
  echo "deploy-staging survived a compose config failure" >&2; exit 1
fi
DOCKER_FAIL=
grep -Fx old-compose "$STG/compose.staging.yml"
grep -Fx old-player "$STG/player/index.html"
if grep -q 'stop api orchestrator caddy' "$DOCKER_LOG"; then
  echo "config failure stopped services it never started" >&2; exit 1
fi
if grep -q 'up -d --force-recreate --no-deps api orchestrator caddy' "$DOCKER_LOG"; then
  echo "pre-activation rollback unnecessarily recreated the running release" >&2; exit 1
fi
if grep -q 'rolling back' "$TMP/config.err"; then
  echo "untouched preflight failure replaced the active files from a snapshot" >&2; exit 1
fi
test ! -e "$STG/.staging-rollback.tar.gz"

# Partial active-file mutation is different from untouched preflight: restore
# every prior file and recreate the old services so Caddy rebinds the restored
# player directory, even though ACTIVATED never became 1.
new_fixture partial-activation-failure
MV_FAIL=activate-caddy
DOCKER_EXPECT_RESTART_IMAGE=prev-image
set +e
run_deploy 2> "$TMP/partial-activation.err"
partial_activation_status=$?
set -e
MV_FAIL=
DOCKER_EXPECT_RESTART_IMAGE=
test "$partial_activation_status" -eq 72 || {
  echo "partial activation failure returned $partial_activation_status, not 72" >&2; exit 1;
}
grep -Fq 'rolling back' "$TMP/partial-activation.err"
grep -Fx 'SHIZZLE_API_IMAGE=prev-image' "$STG/.env"
grep -Fx old-compose "$STG/compose.staging.yml"
grep -Fx old-caddy "$STG/Caddyfile"
grep -Fx old-player "$STG/player/index.html"
grep -Fq 'up -d --force-recreate --no-deps api orchestrator caddy' "$DOCKER_LOG"
if grep -q 'stop api orchestrator caddy' "$DOCKER_LOG"; then
  echo "partial file activation stopped services before the failed release was active" >&2; exit 1
fi
test ! -e "$STG/.staging-rollback.tar.gz"

# Migration failure: services stopped FIRST (with the failed release still
# active), then prior files restored, then the previous image restarted. The
# stub enforces the active identity at both points, so reordering stop after
# the restore (or restarting before it) fails the deploy with 36/37 instead
# of the expected 32.
new_fixture migration-failure
DOCKER_FAIL=migration
DOCKER_EXPECT_STOP_IMAGE="$IMAGE"
DOCKER_EXPECT_RESTART_IMAGE=prev-image
set +e
run_deploy 2> "$TMP/migration.err"
migration_status=$?
set -e
DOCKER_FAIL=
DOCKER_EXPECT_STOP_IMAGE=
DOCKER_EXPECT_RESTART_IMAGE=
test "$migration_status" -eq 32 || { echo "migration-failure rollback produced status $migration_status, not 32 — was the stop reordered after the restore?" >&2; exit 1; }
grep -Fx old-compose "$STG/compose.staging.yml"
grep -Fx old-caddy "$STG/Caddyfile"
grep -Fx old-player "$STG/player/index.html"
if grep -q 'wrong active image' "$DOCKER_LOG"; then
  echo "rollback ran against the wrong release identity" >&2; exit 1
fi
stop_line=$(grep -n 'stop api orchestrator caddy' "$DOCKER_LOG" | head -1 | cut -d: -f1)
restart_line=$(grep -n 'up -d --force-recreate --no-deps api orchestrator caddy' "$DOCKER_LOG" | head -1 | cut -d: -f1)
: "${stop_line:?stop not logged after migration failure}" "${restart_line:?restart not logged after migration failure}"
[ "$stop_line" -lt "$restart_line" ] || {
  echo "rollback restarted the previous image before stopping the failed one" >&2; exit 1
}
test ! -e "$STG/.staging-rollback.tar.gz"

# A green API health response can be caused by the previous orchestrator's
# still-recent heartbeat. If the replacement never writes its own row, the
# deploy must fail and roll back instead of deleting the recovery snapshot.
new_fixture old-heartbeat-only
DOCKER_HEARTBEAT_MODE=old-only
DOCKER_EXPECT_STOP_IMAGE="$IMAGE"
DOCKER_EXPECT_RESTART_IMAGE=prev-image
set +e
run_deploy 2> "$TMP/old-heartbeat.err"
old_heartbeat_status=$?
set -e
DOCKER_HEARTBEAT_MODE=new
DOCKER_EXPECT_STOP_IMAGE=
DOCKER_EXPECT_RESTART_IMAGE=
test "$old_heartbeat_status" -eq 1 || {
  echo "old-heartbeat-only deploy returned $old_heartbeat_status, not 1" >&2; exit 1;
}
grep -Fq 'has not written its service heartbeat' "$TMP/old-heartbeat.err"
grep -Fx 'SHIZZLE_API_IMAGE=prev-image' "$STG/.env"
grep -Fx old-compose "$STG/compose.staging.yml"
grep -Fx old-player "$STG/player/index.html"
test ! -e "$STG/.staging-rollback.tar.gz"

# Health-check failure on an existing release: same stop-before-restore
# order with identity enforcement, prior image identity and files restored.
new_fixture health-failure
CURL_MODE=bad
DOCKER_EXPECT_STOP_IMAGE="$IMAGE"
DOCKER_EXPECT_RESTART_IMAGE=prev-image
set +e
run_deploy 2> "$TMP/health.err"
health_status=$?
set -e
CURL_MODE=ok
DOCKER_EXPECT_STOP_IMAGE=
DOCKER_EXPECT_RESTART_IMAGE=
test "$health_status" -eq 1 || { echo "health-failure rollback produced status $health_status, not 1 — was the stop reordered after the restore?" >&2; exit 1; }
grep -Fx 'SHIZZLE_API_IMAGE=prev-image' "$STG/.env"
grep -Fx old-compose "$STG/compose.staging.yml"
grep -Fx old-player "$STG/player/index.html"
if grep -q 'wrong active image' "$DOCKER_LOG"; then
  echo "rollback ran against the wrong release identity" >&2; exit 1
fi
stop_line=$(grep -n 'stop api orchestrator caddy' "$DOCKER_LOG" | head -1 | cut -d: -f1)
restart_line=$(grep -n 'up -d --force-recreate --no-deps api orchestrator caddy' "$DOCKER_LOG" | head -1 | cut -d: -f1)
[ -n "$stop_line" ] && [ -n "$restart_line" ] && [ "$stop_line" -lt "$restart_line" ] || {
  echo "rollback order wrong on health failure" >&2; exit 1
}
test ! -e "$STG/.staging-rollback.tar.gz"

# Restore-directory creation failure must not be converted into rollback
# success: the failed release stays in place and the snapshot is retained.
new_fixture rollback-mktemp-failure
CURL_MODE=bad
MKTEMP_FAIL_PATTERN=.staging-restore.
set +e
run_deploy 2> "$TMP/rollback-mktemp.err"
rollback_mktemp_status=$?
set -e
CURL_MODE=ok
MKTEMP_FAIL_PATTERN=
test "$rollback_mktemp_status" -ne 0
grep -Fq 'Rollback could not create a restore directory' "$TMP/rollback-mktemp.err"
test -f "$STG/.staging-rollback.tar.gz"
grep -Fx "SHIZZLE_API_IMAGE=$IMAGE" "$STG/.env"
grep -Fx new-player "$STG/player/index.html"

# A failed restore mv likewise fails the deployment and preserves the only
# prior-release archive. The old implementation continued and deleted it.
new_fixture rollback-mv-failure
CURL_MODE=bad
MV_FAIL=restore-env
set +e
run_deploy 2> "$TMP/rollback-mv.err"
rollback_mv_status=$?
set -e
CURL_MODE=ok
MV_FAIL=
test "$rollback_mv_status" -ne 0
grep -Fq 'Rollback failed to restore .env' "$TMP/rollback-mv.err"
test -f "$STG/.staging-rollback.tar.gz"
grep -Fx "SHIZZLE_API_IMAGE=$IMAGE" "$STG/.env"

# Restart failure is reported after files are restored, and retains the
# snapshot so a later run cannot overwrite the unclosed recovery state.
new_fixture rollback-restart-failure
CURL_MODE=bad
DOCKER_FAIL=restart
DOCKER_EXPECT_STOP_IMAGE="$IMAGE"
DOCKER_EXPECT_RESTART_IMAGE=prev-image
set +e
run_deploy 2> "$TMP/rollback-restart.err"
rollback_restart_status=$?
set -e
CURL_MODE=ok
DOCKER_FAIL=
DOCKER_EXPECT_STOP_IMAGE=
DOCKER_EXPECT_RESTART_IMAGE=
test "$rollback_restart_status" -eq 38 || {
  echo "rollback restart failure returned $rollback_restart_status, not 38" >&2; exit 1;
}
grep -Fq 'could not restart the previous release (status 38)' "$TMP/rollback-restart.err"
grep -Fx 'SHIZZLE_API_IMAGE=prev-image' "$STG/.env"
grep -Fx old-compose "$STG/compose.staging.yml"
grep -Fx old-player "$STG/player/index.html"
test -f "$STG/.staging-rollback.tar.gz"

# A failing stop during rollback is surfaced (exit status and message), with
# the prior files still restored; the identity check ran first, so the stop
# attempt is still proven to target the failed release.
new_fixture stop-failure
CURL_MODE=bad
DOCKER_FAIL=stop
DOCKER_EXPECT_STOP_IMAGE="$IMAGE"
DOCKER_EXPECT_RESTART_IMAGE=prev-image
set +e
run_deploy 2> "$TMP/stop.err"
stop_status=$?
set -e
CURL_MODE=ok
DOCKER_FAIL=
DOCKER_EXPECT_STOP_IMAGE=
DOCKER_EXPECT_RESTART_IMAGE=
test "$stop_status" -eq 35 || { echo "stop-failure case produced status $stop_status, not 35" >&2; exit 1; }
grep -Fq 'could not stop the failed services' "$TMP/stop.err"
grep -Fx old-compose "$STG/compose.staging.yml"
grep -Fx 'SHIZZLE_API_IMAGE=prev-image' "$STG/.env"
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
# Reserved characters remain raw for Postgres and percent-encoded in the
# explicit application DSN. This is the p@ss:word/#% regression case.
RAW_DB_PASSWORD='p@ss:word/#%'
ENCODED_DB_PASSWORD='p%40ss%3Aword%2F%23%25'
DATABASE_URL="postgresql+asyncpg://shizzle:$ENCODED_DB_PASSWORD@postgres:5432/shizzle"
cat > "$CFG/.env" <<EOF
POSTGRES_PASSWORD=$RAW_DB_PASSWORD
DATABASE_URL=$DATABASE_URL
SHIZZLE_PASSCODE=test-passcode
TOKEN_SIGNING_SECRET=test-token-signing-secret
SHIZZLE_API_IMAGE=$IMAGE
SHIZZLE_ADMIN_PROXY_SECRET=test-admin-secret
EOF
(
  cd "$CFG"
  docker compose -p shizzle-staging --env-file ./.env -f compose.staging.yml config -q
  docker compose -p shizzle-staging --env-file ./.env -f compose.staging.yml config --format json \
    > "$CFG/staging-config.json"
)
# Every deliberately empty template credential has a Compose :? guard: a
# copied example cannot deploy with public placeholder or disabled auth.
for required_var in POSTGRES_PASSWORD DATABASE_URL SHIZZLE_PASSCODE TOKEN_SIGNING_SECRET; do
  cp "$CFG/.env" "$CFG/bad.env"
  sed -i "s|^$required_var=.*|$required_var=|" "$CFG/bad.env"
  if (cd "$CFG" && docker compose -p shizzle-staging --env-file ./bad.env -f compose.staging.yml config -q) >/dev/null 2>&1; then
    echo "staging compose accepted an empty $required_var" >&2
    exit 1
  fi
done
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
    env = svc.get("environment") or {}
    if name in ("api", "caddy"):
        assert env.get("SHIZZLE_ADMIN_PROXY_SECRET") == "test-admin-secret", name
    if name in ("api", "orchestrator"):
        assert env.get("DATABASE_URL") == (
            "postgresql+asyncpg://shizzle:p%40ss%3Aword%2F%23%25@postgres:5432/shizzle"
        ), (name, env.get("DATABASE_URL"))
    if name == "postgres":
        assert env.get("POSTGRES_PASSWORD") == "p@ss:word/#%", env.get("POSTGRES_PASSWORD")
assert published == [("caddy", "127.0.0.1", "18080", "80")], published
print("staging compose config: only 127.0.0.1:18080 published, limits on every service; "
      "reserved-character DB password stays raw for Postgres and encoded for applications")
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

# Live admin-routing behavior. Upstream stubs answer:
#   209 when X-Shizzle-Admin equals the secret,
#   208 when the header is present with any other value,
#   200 when the header is absent,
# so status codes alone prove stripping (any presence would flip 200→208/209)
# and exact-value injection. Site addresses and the /cdn upstream are the only
# edits: public hostnames become a plain :18082 listener (TLS/ACME is
# untestable here) with the tls block dropped, and the CloudFront host becomes
# a stub container, so the strip can be proven on /api, /ws AND /cdn.
STUB_API="$TMP/stub-api.Caddyfile"
cat > "$STUB_API" <<'EOF'
:8001 {
	@exact header X-Shizzle-Admin test-admin-secret
	@any header X-Shizzle-Admin *
	respond @exact "admin-exact" 209
	respond @any "admin-present" 208
	respond "no-admin" 200
}
EOF
STUB_CDN="$TMP/stub-cdn.Caddyfile"
sed 's/:8001/:9001/' "$STUB_API" > "$STUB_CDN"
BEHAVIOR_PROD="$TMP/Caddyfile.prod.behavior"
sed -e 's/^shizzle\.systems, www\.shizzle\.systems {/:18082 {/' \
  -e $'/^\ttls {$/,/^\t}$/d' \
  -e 's|https://d2488k8kjndpsy\.cloudfront\.net|http://cdn-stub:9001|' \
  -e '/header_up Host d2488k8kjndpsy\.cloudfront\.net$/d' \
  "$CADDY_PROD" > "$BEHAVIOR_PROD"
NET=shizzle-staging-caddytest
docker network create "$NET"
cleanup() {
  docker rm -f staging-config-player-bind db-url-target stub-api stub-cdn caddy-prod-behavior caddy-staging-behavior >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap 'cleanup; rm -rf -- "$TMP"' EXIT

# Live credential regression: authenticate to a real PostgreSQL server with
# the exact percent-encoded DSN under test (libpq and asyncpg apply the same
# URI userinfo decoding). No raw-password URI and no PGPASSWORD fallback.
docker run -d --name db-url-target --network "$NET" --network-alias postgres \
  -e POSTGRES_USER=shizzle -e "POSTGRES_PASSWORD=$RAW_DB_PASSWORD" -e POSTGRES_DB=shizzle \
  postgres:16-alpine >/dev/null
db_ready=0
for attempt in $(seq 1 30); do
  if docker exec db-url-target pg_isready -U shizzle -d shizzle >/dev/null 2>&1; then
    db_ready=1
    break
  fi
  sleep 1
done
[ "$db_ready" -eq 1 ] || { echo "database URL test Postgres did not become ready" >&2; exit 1; }
PSQL_DATABASE_URL=${DATABASE_URL/postgresql+asyncpg/postgresql}
db_identity=$(docker run --rm --network "$NET" -e TEST_DATABASE_URL="$PSQL_DATABASE_URL" \
  postgres:16-alpine sh -c 'psql "$TEST_DATABASE_URL" -Atqc "SELECT current_user, current_database();"')
[ "$db_identity" = 'shizzle|shizzle' ] || {
  echo "encoded DATABASE_URL authenticated as unexpected identity: $db_identity" >&2; exit 1;
}
docker rm -f db-url-target >/dev/null
printf 'encoded DATABASE_URL authenticated to PostgreSQL with reserved-character raw password\n'

docker run -d --name stub-api --network "$NET" --network-alias api \
  -v "$STUB_API":/etc/caddy/Caddyfile:ro caddy:2 >/dev/null
docker run -d --name stub-cdn --network "$NET" --network-alias cdn-stub \
  -v "$STUB_CDN":/etc/caddy/Caddyfile:ro caddy:2 >/dev/null
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

# Public site: admin paths 404 at the edge; the header never reaches any
# upstream — not /api, not /ws, not the /cdn proxy — even when it carries the
# exact secret. (208/209 from a stub would mean the header leaked through.)
[ "$(code http://127.0.0.1:18082/api/admin)" = 404 ]
[ "$(code http://127.0.0.1:18082/api/admin/x)" = 404 ]
[ "$(code http://127.0.0.1:18082/admin)" = 404 ]
[ "$(code http://127.0.0.1:18082/admin/sub/page)" = 404 ]
[ "$(code http://127.0.0.1:18082/api/health)" = 200 ]
[ "$(code -H 'X-Shizzle-Admin: test-admin-secret' http://127.0.0.1:18082/api/health)" = 200 ]
[ "$(code -H 'X-Shizzle-Admin: forged-value' http://127.0.0.1:18082/api/health)" = 200 ]
[ "$(code -H 'X-Shizzle-Admin: test-admin-secret' http://127.0.0.1:18082/ws/relay)" = 200 ]
[ "$(code http://127.0.0.1:18082/cdn/tracks/x)" = 200 ]
[ "$(code -H 'X-Shizzle-Admin: test-admin-secret' http://127.0.0.1:18082/cdn/tracks/x)" = 200 ]
# Tailnet-only admin listener: the exact secret is injected on /api and /ws
# (forged values are overwritten), and admin routes proxy through.
[ "$(code http://127.0.0.1:18081/api/health)" = 209 ]
[ "$(code -H 'X-Shizzle-Admin: forged-value' http://127.0.0.1:18081/api/health)" = 209 ]
[ "$(code http://127.0.0.1:18081/api/admin/x)" = 209 ]
[ "$(code http://127.0.0.1:18081/ws/relay)" = 209 ]
# The secret must never be injected on the /cdn upstream (209 would be a leak).
[ "$(code http://127.0.0.1:18081/cdn/tracks/x)" = 200 ]
# Staging listener: same injection, so /admin works on staging.
[ "$(code http://127.0.0.1:18080/api/health)" = 209 ]
[ "$(code http://127.0.0.1:18080/api/admin/x)" = 209 ]
[ "$(code http://127.0.0.1:18080/ws/relay)" = 209 ]

# N3: with persist_config off, the env-substituted admin secret must never be
# written under /config or /data (the prod behavior container runs the
# sed-derived copy of the real Caddyfile including its global options block;
# the staging container runs the real file).
for c in caddy-prod-behavior caddy-staging-behavior; do
  found=$(docker exec "$c" sh -c 'grep -R -s test-admin-secret /config /data' 2>/dev/null || true)
  [ -z "$found" ] || { echo "admin secret persisted under /config or /data in $c" >&2; exit 1; }
  docker exec "$c" sh -c 'test ! -e /config/caddy/autosave.json' \
    || { echo "autosave.json written in $c despite persist_config off" >&2; exit 1; }
done

# Verifier regression: an untouched preflight failure must leave the existing
# player's bind mount attached. Recreating or renaming that directory makes
# /srv/index.html disappear in the already-running container even when the
# host copy still says old-player. The same container ID must survive and
# read the original content after the config failure, with no restart call.
new_fixture bind-config-failure
docker run -d --name staging-config-player-bind \
  -v "$STG/player:/srv:ro" caddy:2 sh -c 'while :; do sleep 300; done' >/dev/null
bind_id_before=$(docker inspect --format '{{.Id}}' staging-config-player-bind)
[ "$(docker exec staging-config-player-bind cat /srv/index.html)" = old-player ]
DOCKER_FAIL=config
set +e
run_deploy 2> "$TMP/bind-config.err"
bind_config_status=$?
set -e
DOCKER_FAIL=
test "$bind_config_status" -eq 30 || {
  echo "bind-mounted config failure returned $bind_config_status, not 30" >&2; exit 1;
}
bind_id_after=$(docker inspect --format '{{.Id}}' staging-config-player-bind)
[ "$bind_id_after" = "$bind_id_before" ] || {
  echo "untouched config failure replaced the running player container" >&2; exit 1;
}
[ "$(docker exec staging-config-player-bind cat /srv/index.html)" = old-player ] || {
  echo "running container lost the original player bind mount after config failure" >&2; exit 1;
}
if grep -q 'up -d --force-recreate --no-deps api orchestrator caddy' "$DOCKER_LOG"; then
  echo "bind-mounted config failure requested an unnecessary restart" >&2; exit 1
fi
test ! -e "$STG/.staging-rollback.tar.gz"
docker rm -f staging-config-player-bind >/dev/null
printf 'untouched config failure preserved the running player bind mount\n'

printf 'docker-gated staging and admin-routing checks passed\n'
