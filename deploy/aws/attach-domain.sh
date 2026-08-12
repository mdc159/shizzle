#!/usr/bin/env bash
# Attach the shizzle.systems alias + ACM certificate to the production
# distribution. Run ONLY after:
#   1. 02-request-cert.sh has run (writes cert-arn.txt), and
#   2. the certificate shows ISSUED (check with ./poll-cert.sh).
#
# CloudFront rejects an alias whose cert is still PENDING_VALIDATION, which is
# why distribution creation (03) and domain attachment (this script) are two
# separate steps.
set -euo pipefail
cd "$(dirname "$0")"
source ./env.sh

DIST_ID=$(cat distribution-id.txt)
CERT_ARN=$(cat cert-arn.txt)

STATUS=$(aws acm describe-certificate --region us-east-1 --certificate-arn "$CERT_ARN" \
  --query 'Certificate.Status' --output text)
if [[ "$STATUS" != "ISSUED" ]]; then
  echo "Certificate is $STATUS, not ISSUED — cannot attach alias yet." >&2
  exit 1
fi

# get-distribution-config returns {ETag, DistributionConfig}; update needs both.
aws cloudfront get-distribution-config --id "$DIST_ID" > dist-current.json
ETAG=$(python -c 'import json; print(json.load(open("dist-current.json"))["ETag"])')
python -c "
import json
doc = json.load(open('dist-current.json'))
cfg = doc['DistributionConfig']
cfg['Aliases'] = {'Quantity': 1, 'Items': ['$DOMAIN']}
cfg['ViewerCertificate'] = {
    'ACMCertificateArn': '$CERT_ARN',
    'SSLSupportMethod': 'sni-only',
    'MinimumProtocolVersion': 'TLSv1.2_2021',
    'Certificate': '$CERT_ARN',
    'CertificateSource': 'acm',
}
json.dump(cfg, open('dist-updated.json', 'w'))
"
aws cloudfront update-distribution --id "$DIST_ID" --if-match "$ETAG" \
  --distribution-config file://dist-updated.json \
  --query 'Distribution.{Id:Id,Status:Status,Aliases:DistributionConfig.Aliases.Items}' --output json
rm -f dist-current.json dist-updated.json

echo "Alias attached. Next: ./04-dns-alias.sh"
