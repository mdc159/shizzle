#!/bin/bash
# One-shot data repair, EXECUTED on the VPS 2026-08-04 — kept as the record.
#
# "The Pot" (f995371a-7d9b-5dbb-82d2-0000dc80aacf) was imported with 6 x
# 95.5 MiB raw WAV stems (573 MiB total; sourced from karaoke/out/ instead of
# pub/), which wedged the player's full-preload gate. This script, run as
# root on the VPS (datacenter bandwidth):
#   phase1: pulled the WAVs from S3, transcoded to 320k AAC .m4a (faststart,
#           matching worker output), uploaded with Content-Type audio/mp4,
#           rewrote manifest.json stems/*.wav -> stems/*.m4a
#   phase2: ffprobe-verified every .m4a, then deleted the WAV objects
# Result: stems now 11.3-14.0 MiB each (378.6 s track), live manifest serves
# .m4a, end-to-end playback verified with Playwright against prod.
# Recurrence is blocked by publish.validate_stem_object (same commit series).
set -euo pipefail

PHASE="${1:-phase1}"
BUCKET=karaoke-pimpshizzle
TRACK=f995371a-7d9b-5dbb-82d2-0000dc80aacf
PREFIX="tracks/$TRACK/1"
WORK=/tmp/thepot
STEMS="vocals drums bass guitar piano shizzle"

export $(grep -E "^(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY)=" /opt/shizzle/prod/.env | tr -d "\r" | xargs)
export AWS_DEFAULT_REGION=us-east-1
unset AWS_ENDPOINT_URL || true

s3() {
  docker run --rm \
    -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_DEFAULT_REGION \
    -v "$WORK":/work amazon/aws-cli "$@"
}

mkdir -p "$WORK"

if [ "$PHASE" = "phase1" ]; then
  echo "=== BEFORE: listing $PREFIX ==="
  s3 s3 ls "s3://$BUCKET/$PREFIX/" --recursive

  echo "=== downloading manifest + wav stems ==="
  s3 s3 cp "s3://$BUCKET/$PREFIX/manifest.json" /work/manifest.json
  for s in $STEMS; do
    s3 s3 cp "s3://$BUCKET/$PREFIX/stems/$s.wav" "/work/$s.wav"
  done
  ls -l "$WORK"

  echo "=== transcoding to 320k AAC m4a (faststart) ==="
  for s in $STEMS; do
    ffmpeg -hide_banner -loglevel error -y -i "$WORK/$s.wav" \
      -c:a aac -b:a 320k -movflags +faststart "$WORK/$s.m4a"
    echo "$s: $(stat -c%s "$WORK/$s.wav") -> $(stat -c%s "$WORK/$s.m4a") bytes"
  done

  echo "=== uploading m4a stems (Content-Type audio/mp4) ==="
  for s in $STEMS; do
    s3 s3 cp "/work/$s.m4a" "s3://$BUCKET/$PREFIX/stems/$s.m4a" \
      --content-type audio/mp4
  done

  echo "=== rewriting manifest ==="
  cp "$WORK/manifest.json" "$WORK/manifest.json.bak"
  sed 's#stems/\([a-z]*\)\.wav#stems/\1.m4a#g' "$WORK/manifest.json.bak" > "$WORK/manifest.json"
  echo "--- manifest diff ---"
  diff "$WORK/manifest.json.bak" "$WORK/manifest.json" || true
  s3 s3 cp /work/manifest.json "s3://$BUCKET/$PREFIX/manifest.json" \
    --content-type application/json

  echo "=== AFTER: verify m4a in place ==="
  for s in $STEMS; do
    s3 s3api head-object --bucket "$BUCKET" --key "$PREFIX/stems/$s.m4a" \
      --query "[ContentLength,ContentType]" --output text
  done
  s3 s3 cp "s3://$BUCKET/$PREFIX/manifest.json" - | grep '"file"'
  echo "PHASE1 DONE"
elif [ "$PHASE" = "phase2" ]; then
  echo "=== verifying every m4a decodes before deleting wavs ==="
  for s in $STEMS; do
    d=$(ffprobe -v error -show_entries format=duration,format_name -of default=nw=1 "$WORK/$s.m4a")
    echo "$s.m4a: $d"
  done
  echo "=== deleting wav objects ==="
  for s in $STEMS; do
    s3 s3 rm "s3://$BUCKET/$PREFIX/stems/$s.wav"
  done
  echo "=== FINAL listing ==="
  s3 s3 ls "s3://$BUCKET/$PREFIX/" --recursive
  echo "PHASE2 DONE"
fi
