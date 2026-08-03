#!/usr/bin/env bash
# Upsert Route 53 alias A + AAAA records: shizzle.systems -> production
# CloudFront distribution.
#
# Run AFTER ./attach-domain.sh — CloudFront serves an alias only when it is
# attached to the distribution; pointing DNS earlier just yields CloudFront
# 403 "not a configured alias" errors.
set -euo pipefail
cd "$(dirname "$0")"
source ./env.sh

DIST_ID=$(cat distribution-id.txt)
DIST_DOMAIN=$(cat distribution-domain.txt)
CF_ALIAS_ZONE="Z2FDTNDATAQYW2"   # fixed hosted zone ID for ALL CloudFront distributions

# Guard: refuse unless the alias is actually attached.
ATTACHED=$(aws cloudfront get-distribution-config --id "$DIST_ID" \
  --query 'DistributionConfig.Aliases.Items' --output text)
if [[ "$ATTACHED" != *"$DOMAIN"* ]]; then
  echo "Alias $DOMAIN is not attached to $DIST_ID yet — run ./attach-domain.sh first." >&2
  exit 1
fi

for TYPE in A AAAA; do
  aws route53 change-resource-record-sets --hosted-zone-id "$HOSTED_ZONE_ID" --change-batch "{
    \"Comment\": \"shizzle production: apex alias to CloudFront\",
    \"Changes\": [{\"Action\": \"UPSERT\", \"ResourceRecordSet\": {
      \"Name\": \"$DOMAIN.\", \"Type\": \"$TYPE\",
      \"AliasTarget\": {
        \"HostedZoneId\": \"$CF_ALIAS_ZONE\",
        \"DNSName\": \"$DIST_DOMAIN\",
        \"EvaluateTargetHealth\": false
      }}]}
  }" --query 'ChangeInfo.Status' --output text
done

echo "Alias records upserted: $DOMAIN -> $DIST_DOMAIN (A + AAAA)"
