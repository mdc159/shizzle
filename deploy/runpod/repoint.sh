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
[[ "$ENDPOINT_ID" =~ ^[A-Za-z0-9-]+$ ]] || { echo "endpoint_id contains invalid characters" >&2; exit 2; }
[[ "$SOURCE_TEMPLATE_ID" =~ ^[A-Za-z0-9-]+$ ]] || { echo "template_id contains invalid characters" >&2; exit 2; }
TAG_REF="ghcr.io/mdc159/shizzle/worker:$IMAGE_TAG"
MANIFEST=$("$DOCKER_BIN" buildx imagetools inspect "$TAG_REF" --format '{{json .Manifest}}') || {
  echo "Worker image does not exist: $TAG_REF" >&2
  exit 1
}
DIGEST=$(jq -er '.digest | select(test("^sha256:[0-9a-f]{64}$"))' <<<"$MANIFEST") || {
  echo "Registry returned no valid manifest digest for $TAG_REF" >&2
  exit 1
}
IMAGE="ghcr.io/mdc159/shizzle/worker@$DIGEST"

WORK_DIR=$(mktemp -d)
cd "$WORK_DIR"
AUTH_HEADER="$WORK_DIR/authorization.header"
printf 'Authorization: Bearer %s\n' "$RUNPOD_API_KEY" > "$AUTH_HEADER"
chmod 600 "$AUTH_HEADER"
auth=(-H "@$AUTH_HEADER")
NEW_TEMPLATE_ID=
BOUND=0
UPDATE_STARTED=0
cleanup() {
  status=$?
  if [ "$BOUND" -ne 1 ] && [ -n "$NEW_TEMPLATE_ID" ]; then
    DELETE_UNBOUND=1
    if [ "$UPDATE_STARTED" -eq 1 ]; then
      DELETE_UNBOUND=0
      if "$CURL_BIN" -fsS "${auth[@]}" "$API_BASE/endpoints/$ENDPOINT_ID" > endpoint-reconcile.json; then
        if ! RECONCILED_TEMPLATE=$(jq -er '.templateId | select(type == "string" and length > 0)' endpoint-reconcile.json); then
          echo "Endpoint reconciliation returned no valid template ID; preserved template $NEW_TEMPLATE_ID" >&2
        elif [ "$RECONCILED_TEMPLATE" = "$NEW_TEMPLATE_ID" ]; then
          BOUND=1
          echo "Endpoint update result was ambiguous; preserved bound template $NEW_TEMPLATE_ID" >&2
        else
          echo "Endpoint reconciliation may be stale; preserved template $NEW_TEMPLATE_ID" >&2
        fi
      else
        echo "Could not reconcile endpoint update; preserved template $NEW_TEMPLATE_ID" >&2
      fi
    fi
    if [ "$BOUND" -ne 1 ] && [ "$DELETE_UNBOUND" -eq 1 ]; then
      "$CURL_BIN" -fsS -X DELETE "${auth[@]}" "$API_BASE/templates/$NEW_TEMPLATE_ID" >/dev/null \
        || echo "Failed to delete unbound template $NEW_TEMPLATE_ID" >&2
    fi
  fi
  rm -rf -- "$WORK_DIR"
  exit "$status"
}
trap cleanup EXIT

"$CURL_BIN" -fsS "${auth[@]}" "$API_BASE/endpoints/$ENDPOINT_ID" > endpoint-before.json
"$CURL_BIN" -fsS "${auth[@]}" \
  "$API_BASE/templates/$SOURCE_TEMPLATE_ID?includeEndpointBoundTemplates=true" > template-before.json
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
UPDATE_STARTED=1
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
