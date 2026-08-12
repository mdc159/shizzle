#!/usr/bin/env bash
# Request the production ACM certificate for shizzle.systems + *.shizzle.systems
# (us-east-1 — CloudFront requirement) and upsert the DNS validation CNAME(s)
# into the hosted zone. Idempotent-ish: reuses an existing non-failed cert for
# the domain if one exists.
#
# Writes the certificate ARN to deploy/aws/cert-arn.txt for later steps.
set -euo pipefail
cd "$(dirname "$0")"
source ./env.sh

ARN_FILE="cert-arn.txt"

# Reuse existing cert if present (ISSUED or PENDING_VALIDATION)
EXISTING=$(aws acm list-certificates --region us-east-1 \
  --certificate-statuses ISSUED PENDING_VALIDATION \
  --query "CertificateSummaryList[?DomainName=='$DOMAIN'].CertificateArn" --output text)

if [[ -n "$EXISTING" && "$EXISTING" != "None" ]]; then
  CERT_ARN="$EXISTING"
  echo "Reusing existing certificate: $CERT_ARN"
else
  CERT_ARN=$(aws acm request-certificate --region us-east-1 \
    --domain-name "$DOMAIN" \
    --subject-alternative-names "*.$DOMAIN" \
    --validation-method DNS \
    --tags "Key=project,Value=shizzle" "Key=purpose,Value=shizzle production" \
    --query CertificateArn --output text)
  echo "Requested certificate: $CERT_ARN"
fi
echo "$CERT_ARN" > "$ARN_FILE"

# ACM takes a few seconds to populate the validation records
echo "Waiting for validation records to populate..."
for i in $(seq 1 12); do
  RECORDS=$(aws acm describe-certificate --region us-east-1 --certificate-arn "$CERT_ARN" \
    --query 'Certificate.DomainValidationOptions[].ResourceRecord.{Name:Name,Value:Value}' --output json)
  if [[ "$RECORDS" != "[]" ]] && ! echo "$RECORDS" | grep -q null; then
    break
  fi
  sleep 5
done

if [[ "$RECORDS" == "[]" ]] || echo "$RECORDS" | grep -q null; then
  echo "FATAL: validation records never populated" >&2
  exit 1
fi

# Upsert each unique validation CNAME into the hosted zone.
# (apex + wildcard usually share ONE record — dedupe.)
echo "$RECORDS" | python -c '
import json, sys
records = json.load(sys.stdin)
seen, changes = set(), []
for r in records:
    key = (r["Name"], r["Value"])
    if key in seen:
        continue
    seen.add(key)
    changes.append({
        "Action": "UPSERT",
        "ResourceRecordSet": {
            "Name": r["Name"],
            "Type": "CNAME",
            "TTL": 300,
            "ResourceRecords": [{"Value": r["Value"]}],
        },
    })
print(json.dumps({"Comment": "ACM validation for shizzle production cert", "Changes": changes}))
' > acm-validation-batch.json

aws route53 change-resource-record-sets \
  --hosted-zone-id "$HOSTED_ZONE_ID" \
  --change-batch file://acm-validation-batch.json \
  --query 'ChangeInfo.{Id:Id,Status:Status}' --output table

echo
echo "Certificate status:"
aws acm describe-certificate --region us-east-1 --certificate-arn "$CERT_ARN" \
  --query 'Certificate.{Status:Status,Domains:SubjectAlternativeNames}' --output json
echo
echo "NOTE: validation requires shizzle.systems to resolve publicly."
echo "If the domain registration/TLD delegation has not propagated yet, the cert"
echo "stays PENDING_VALIDATION and auto-completes once DNS goes live (ACM re-checks"
echo "for 72h). Poll with: ./poll-cert.sh"
