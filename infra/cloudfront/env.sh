# shellcheck shell=bash
# Shared environment loader for shizzle production CDN scripts.
# Usage: source "$(dirname "$0")/env.sh"
#
# Loads AWS credentials from the repo .env and clears the machine-level
# AWS_ENDPOINT_URL (R2 override) so the AWS CLI talks to real AWS.
# Never prints secret values.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$REPO_ROOT/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "FATAL: $ENV_FILE not found" >&2
  exit 1
fi

_get() { grep -m1 "^$1=" "$ENV_FILE" | cut -d= -f2- | tr -d '\r'; }

export AWS_ACCESS_KEY_ID="$(_get AWS_ACCESS_KEY_ID)"
export AWS_SECRET_ACCESS_KEY="$(_get AWS_SECRET_ACCESS_KEY)"
export AWS_DEFAULT_REGION="us-east-1"   # CloudFront + ACM-for-CloudFront + route53domains all live here
export AWS_REGION="us-east-1"
unset AWS_ENDPOINT_URL AWS_ENDPOINT_URL_S3   # machine-level R2 override must not leak in
export AWS_PAGER=""

# ---- shizzle production constants -------------------------------------------
export DOMAIN="shizzle.systems"
export HOSTED_ZONE_ID="Z07938355FL89IEW1HFO"
export SPIKE_MEDIA_BUCKET="shizzle-spike-media-9abf4c"      # SWAP-AT-PHASE-4: replace with production media bucket
export SPIKE_KEY_GROUP_ID="cfad272c-929b-45be-93db-501dd50e5948"  # shizzle-spike-keygroup (signed cookies)
export VPS_ORIGIN="72.60.173.171"
export PROD_COMMENT="shizzle production"

if [[ -z "$AWS_ACCESS_KEY_ID" || -z "$AWS_SECRET_ACCESS_KEY" ]]; then
  echo "FATAL: AWS credentials missing from .env" >&2
  exit 1
fi
