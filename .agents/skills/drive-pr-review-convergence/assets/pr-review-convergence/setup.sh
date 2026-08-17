#!/usr/bin/env bash
set -euo pipefail

CHECKOUT=${1:?usage: setup.sh CHECKOUT_PATH}
cd "$CHECKOUT"

# Rendered from the required --setup-command arguments. These commands must be
# deterministic, credential-free, non-interactive, and Linux-compatible.
{{SETUP_COMMANDS}}

git --version
uv --version
node --version
npm --version
git status --short
