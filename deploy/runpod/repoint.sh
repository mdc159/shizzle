#!/usr/bin/env bash
set -euo pipefail

IMAGE_TAG=${1:?usage: repoint.sh IMAGE_TAG WORKERS_MAX ENDPOINT_ID SOURCE_TEMPLATE_ID}
WORKERS_MAX=${2:?usage: repoint.sh IMAGE_TAG WORKERS_MAX ENDPOINT_ID SOURCE_TEMPLATE_ID}
ENDPOINT_ID=${3:?usage: repoint.sh IMAGE_TAG WORKERS_MAX ENDPOINT_ID SOURCE_TEMPLATE_ID}
SOURCE_TEMPLATE_ID=${4:?usage: repoint.sh IMAGE_TAG WORKERS_MAX ENDPOINT_ID SOURCE_TEMPLATE_ID}
RUNPOD_API_KEY=${RUNPOD_API_KEY:?RUNPOD_API_KEY is required}
API_BASE=${RUNPOD_API_BASE:-https://rest.runpod.io/v1}
CURL_BIN=${RUNPOD_CURL_BIN:-curl}
DOCKER_BIN=${RUNPOD_DOCKER_BIN:-docker}

[[ "$IMAGE_TAG" =~ ^sha-[0-9a-f]{40}$ ]] || { echo "image_tag must be sha- followed by 40 lowercase hex characters" >&2; exit 2; }
[[ "$WORKERS_MAX" =~ ^[0-9]{1,2}$ ]] || { echo "workers_max must be an integer from 0 to 99" >&2; exit 2; }
IMAGE="ghcr.io/mdc159/shizzle/worker:$IMAGE_TAG"
"$DOCKER_BIN" manifest inspect "$IMAGE" >/dev/null 2>&1 || { echo "Worker image does not exist: $IMAGE" >&2; exit 1; }

WORK_DIR=$(mktemp -d)
cd "$WORK_DIR"
AUTH_HEADER="$WORK_DIR/authorization.header"
printf 'Authorization: Bearer %s\n' "$RUNPOD_API_KEY" > "$AUTH_HEADER"
chmod 600 "$AUTH_HEADER"
auth=(-H "@$AUTH_HEADER")
NEW_TEMPLATE_ID=
BOUND=0
cleanup() {
  status=$?
  if [ "$BOUND" -ne 1 ] && [ -n "$NEW_TEMPLATE_ID" ]; then
    "$CURL_BIN" -fsS -X DELETE "${auth[@]}" "$API_BASE/templates/$NEW_TEMPLATE_ID" >/dev/null \
      || echo "Failed to delete unbound template $NEW_TEMPLATE_ID" >&2
  fi
  rm -rf -- "$WORK_DIR"
  exit "$status"
}
trap cleanup EXIT

"$CURL_BIN" -fsS "${auth[@]}" "$API_BASE/endpoints/$ENDPOINT_ID" > endpoint-before.json
"$CURL_BIN" -fsS "${auth[@]}" "$API_BASE/templates/$SOURCE_TEMPLATE_ID" > template-before.json
test "$(jq -r '.templateId' endpoint-before.json)" = "$SOURCE_TEMPLATE_ID" || {
  echo "Endpoint $ENDPOINT_ID no longer uses source template $SOURCE_TEMPLATE_ID" >&2
  exit 1
}

suffix=${GITHUB_RUN_ID:-manual}-${GITHUB_RUN_ATTEMPT:-1}
name="shizzle-${IMAGE_TAG:4:12}-$suffix"
jq -n --slurpfile old template-before.json --arg image "$IMAGE" --arg name "$name" '
  $old[0] | {
    category, containerDiskInGb, containerRegistryAuthId, dockerEntrypoint,
    dockerStartCmd, env, imageName: $image, isPublic: false,
    isServerless: true, name: $name, ports, readme, volumeInGb,
    volumeMountPath
  } | with_entries(select(.value != null))
' > template-create.json

"$CURL_BIN" -fsS -X POST "${auth[@]}" -H 'Content-Type: application/json' \
  --data @template-create.json "$API_BASE/templates" > template-created.json
NEW_TEMPLATE_ID=$(jq -er '.id' template-created.json)
jq -nc --arg templateId "$NEW_TEMPLATE_ID" --argjson workersMax "$WORKERS_MAX" \
  '{templateId: $templateId, workersMax: $workersMax}' > endpoint-update.json
"$CURL_BIN" -fsS -X POST "${auth[@]}" -H 'Content-Type: application/json' \
  --data @endpoint-update.json "$API_BASE/endpoints/$ENDPOINT_ID/update" > endpoint-updated.json
jq -e --arg template "$NEW_TEMPLATE_ID" --argjson workers "$WORKERS_MAX" \
  '.templateId == $template and .workersMax == $workers' endpoint-updated.json >/dev/null
BOUND=1

jq -n --slurpfile endpoint endpoint-updated.json --slurpfile template template-created.json \
  --arg priorTemplateId "$SOURCE_TEMPLATE_ID" \
  '{id: $endpoint[0].id, priorTemplateId: $priorTemplateId,
    templateId: $endpoint[0].templateId, workersMax: $endpoint[0].workersMax,
    imageName: $template[0].imageName}'
