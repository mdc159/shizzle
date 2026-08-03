#!/usr/bin/env bash
# Check shizzle.systems domain registration status and public NS resolution.
# Safe to run repeatedly; read-only.
set -euo pipefail
source "$(dirname "$0")/env.sh"

echo "== route53domains operations (us-east-1) =="
aws route53domains list-operations --region us-east-1 \
  --query 'Operations[].{Id:OperationId,Type:Type,Status:Status,Domain:DomainName,Submitted:SubmittedDate,Updated:LastUpdatedDate}' \
  --output table

echo
echo "== hosted zone NS (authoritative per Route 53) =="
aws route53 get-hosted-zone --id "$HOSTED_ZONE_ID" \
  --query 'DelegationSet.NameServers' --output text

echo
echo "== public DNS: NS for $DOMAIN via 8.8.8.8 =="
nslookup -type=NS "$DOMAIN" 8.8.8.8 || echo "(no public NS answer yet — registration still settling)"
