#!/usr/bin/env bash
# Create the shizzle PRODUCTION CloudFront distribution.
#
# What this does, in order:
#   1. Upserts vps.shizzle.systems A -> VPS IP (CloudFront origins must be DNS
#      names, never bare IPs — this record IS the workaround).
#   2. Creates (or reuses) the production OAC "shizzle-production-oac".
#   3. Renders distribution-config.template.json and creates the distribution
#      WITHOUT alias/custom cert (default *.cloudfront.net cert). Attaching the
#      shizzle.systems alias requires an ISSUED ACM cert — that is a separate
#      step: ./attach-domain.sh.
#   4. Extends the spike media bucket policy so the NEW distribution ARN can
#      also read it (OAC bucket policies are per-distribution-ARN).
#
# SWAP-AT-PHASE-4 markers:
#   - Origin "s3-media" points at the SPIKE bucket shizzle-spike-media-9abf4c.
#     Swap DomainName + bucket policy to the production media bucket in Phase 4.
#   - Origin "vps-api" is http-only (port 80). Swap OriginProtocolPolicy to
#     https-only once Caddy on the VPS holds a cert for vps.shizzle.systems.
#
# Writes distribution-id.txt + distribution-domain.txt.
set -euo pipefail
cd "$(dirname "$0")"
source ./env.sh

if [[ -f distribution-id.txt ]]; then
  echo "distribution-id.txt already exists ($(cat distribution-id.txt)) — refusing to create a duplicate." >&2
  exit 1
fi

# -- 1. vps origin DNS name ---------------------------------------------------
echo "Upserting vps.$DOMAIN A -> $VPS_ORIGIN"
aws route53 change-resource-record-sets --hosted-zone-id "$HOSTED_ZONE_ID" --change-batch "{
  \"Comment\": \"shizzle production: VPS origin name for CloudFront\",
  \"Changes\": [{\"Action\": \"UPSERT\", \"ResourceRecordSet\": {
    \"Name\": \"vps.$DOMAIN.\", \"Type\": \"A\", \"TTL\": 300,
    \"ResourceRecords\": [{\"Value\": \"$VPS_ORIGIN\"}]}}]
}" --query 'ChangeInfo.Status' --output text

# -- 2. production OAC --------------------------------------------------------
OAC_ID=$(aws cloudfront list-origin-access-controls \
  --query "OriginAccessControlList.Items[?Name=='shizzle-production-oac'].Id" --output text)
if [[ -z "$OAC_ID" || "$OAC_ID" == "None" ]]; then
  OAC_ID=$(aws cloudfront create-origin-access-control --origin-access-control-config \
    'Name=shizzle-production-oac,Description=shizzle production S3 media OAC,SigningProtocol=sigv4,SigningBehavior=always,OriginAccessControlOriginType=s3' \
    --query 'OriginAccessControl.Id' --output text)
  echo "Created OAC: $OAC_ID"
else
  echo "Reusing OAC: $OAC_ID"
fi

# -- 3. distribution ----------------------------------------------------------
CALLER_REF="shizzle-production-$(date +%s)"
sed -e "s/__CALLER_REFERENCE__/$CALLER_REF/" -e "s/__OAC_ID__/$OAC_ID/" \
  distribution-config.template.json > distribution-config.rendered.json

CREATED=$(aws cloudfront create-distribution-with-tags \
  --distribution-config-with-tags file://distribution-config.rendered.json \
  --query 'Distribution.{Id:Id,Domain:DomainName,Status:Status}' --output json)
echo "$CREATED"
DIST_ID=$(echo "$CREATED" | python -c 'import json,sys; print(json.load(sys.stdin)["Id"])')
DIST_DOMAIN=$(echo "$CREATED" | python -c 'import json,sys; print(json.load(sys.stdin)["Domain"])')
echo "$DIST_ID" > distribution-id.txt
echo "$DIST_DOMAIN" > distribution-domain.txt

# -- 4. widen spike bucket policy to include the new distribution -------------
# SWAP-AT-PHASE-4: repoint at the production bucket when it exists.
echo "Extending $SPIKE_MEDIA_BUCKET policy for distribution $DIST_ID"
aws s3api get-bucket-policy --bucket "$SPIKE_MEDIA_BUCKET" --query Policy --output text | \
python -c "
import json, sys
policy = json.loads(sys.stdin.read())
arn = 'arn:aws:cloudfront::826783599575:distribution/$DIST_ID'
stmt = policy['Statement'][0]
cond = stmt['Condition']['StringEquals']
arns = cond['AWS:SourceArn']
if isinstance(arns, str):
    arns = [arns]
if arn not in arns:
    arns.append(arn)
cond['AWS:SourceArn'] = arns
print(json.dumps(policy))
" > bucket-policy.rendered.json
aws s3api put-bucket-policy --bucket "$SPIKE_MEDIA_BUCKET" --policy file://bucket-policy.rendered.json

echo
echo "Distribution $DIST_ID ($DIST_DOMAIN) creating. Deploy takes ~5-15 min:"
echo "  aws cloudfront get-distribution --id $DIST_ID --query Distribution.Status"
echo "Next: ./attach-domain.sh once the ACM cert is ISSUED, then ./04-dns-alias.sh"
