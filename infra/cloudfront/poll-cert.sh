#!/usr/bin/env bash
# Poll the production ACM certificate status. Read-only.
set -euo pipefail
cd "$(dirname "$0")"
source ./env.sh

CERT_ARN=$(cat cert-arn.txt)
aws acm describe-certificate --region us-east-1 --certificate-arn "$CERT_ARN" \
  --query 'Certificate.{Status:Status,ValidationStatus:DomainValidationOptions[0].ValidationStatus,Not_After:NotAfter}' \
  --output table
