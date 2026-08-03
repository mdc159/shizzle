# Spike 0.2 teardown — signed-cookie proof resources

Every resource below was created 2026-08-02 with the `shizzle-spike-` prefix
(and tag `project=shizzle-spike` where the service supports tag-on-create).
The legacy bucket `karaoke-pimpshizzle` was NOT touched — do not delete it.

Run with the shizzle `.env` AWS credentials, region us-east-1, and
`AWS_ENDPOINT_URL` unset (this machine has a global override pointing at R2).

## Resource inventory

| Resource | ID / Name |
|---|---|
| S3 bucket | `shizzle-spike-media-9abf4c` |
| CloudFront distribution | `EYDQD3CPYVRRU` (d2wr9nfx0lr3a2.cloudfront.net) |
| CloudFront key group | `cfad272c-929b-45be-93db-501dd50e5948` (shizzle-spike-keygroup) |
| CloudFront public key | `KRNC9VLVC15DN` (shizzle-spike-key) |
| CloudFront origin access control | `E2FU4GKEQOF0HR` (shizzle-spike-oac) |
| Local private key | `X:\GitHub\shizzle\secrets\cloudfront-spike\` (gitignored) |

## Deletion order (dependencies force this sequence)

CloudFront deletes require the current `ETag` as `--if-match`; fetch it fresh
before each delete. The distribution must be disabled and fully deployed
before it can be deleted (allow 5-15 minutes after the disable).

### 1. Disable the distribution

```sh
# Fetch current config + ETag
aws cloudfront get-distribution-config --id EYDQD3CPYVRRU > dist-config.json
# Edit dist-config.json: set "Enabled": false, note the "ETag" value,
# and strip the outer ETag wrapper so only DistributionConfig remains, then:
aws cloudfront update-distribution --id EYDQD3CPYVRRU \
  --distribution-config file://dist-config-only.json --if-match <ETAG>
aws cloudfront wait distribution-deployed --id EYDQD3CPYVRRU
```

### 2. Delete the distribution

```sh
ETAG=$(aws cloudfront get-distribution --id EYDQD3CPYVRRU --query ETag --output text)
aws cloudfront delete-distribution --id EYDQD3CPYVRRU --if-match "$ETAG"
```

### 3. Delete key group, then public key

```sh
ETAG=$(aws cloudfront get-key-group --id cfad272c-929b-45be-93db-501dd50e5948 --query ETag --output text)
aws cloudfront delete-key-group --id cfad272c-929b-45be-93db-501dd50e5948 --if-match "$ETAG"

ETAG=$(aws cloudfront get-public-key --id KRNC9VLVC15DN --query ETag --output text)
aws cloudfront delete-public-key --id KRNC9VLVC15DN --if-match "$ETAG"
```

### 4. Delete the origin access control

```sh
ETAG=$(aws cloudfront get-origin-access-control --id E2FU4GKEQOF0HR --query ETag --output text)
aws cloudfront delete-origin-access-control --id E2FU4GKEQOF0HR --if-match "$ETAG"
```

### 5. Empty and delete the bucket

```sh
aws s3 rm s3://shizzle-spike-media-9abf4c --recursive
aws s3api delete-bucket --bucket shizzle-spike-media-9abf4c
```

### 6. Local key material

```sh
rm -rf X:/GitHub/shizzle/secrets/cloudfront-spike
```

## Verify nothing is left

```sh
aws cloudfront list-distributions --query "DistributionList.Items[?Id=='EYDQD3CPYVRRU']"
aws s3api head-bucket --bucket shizzle-spike-media-9abf4c   # expect 404
aws cloudfront list-key-groups --query "KeyGroupList.Items[?KeyGroup.KeyGroupConfig.Name=='shizzle-spike-keygroup']"
aws cloudfront list-public-keys --query "PublicKeyList.Items[?Name=='shizzle-spike-key']"
aws cloudfront list-origin-access-controls --query "OriginAccessControlList.Items[?Name=='shizzle-spike-oac']"
```
