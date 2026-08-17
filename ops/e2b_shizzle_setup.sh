#!/usr/bin/env bash
set -euo pipefail

repo="${1:-/workspace/repo}"
test -d "$repo/.git"

uv sync --directory "$repo/library" --frozen
uv sync --directory "$repo/stemsplit" --frozen
npm ci --prefix "$repo/player" --no-audit --no-fund

git -C "$repo" diff --check
test -z "$(git -C "$repo" status --porcelain)"

printf 'Shizzle dependencies are ready at %s\n' "$repo"
