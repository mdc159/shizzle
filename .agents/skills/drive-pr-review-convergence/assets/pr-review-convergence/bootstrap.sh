#!/usr/bin/env bash
set -euo pipefail

for command in git gh uv; do
  command -v "$command" >/dev/null || {
    printf 'missing required host command: %s\n' "$command" >&2
    exit 1
  }
done

e2b_api_key="${E2B_API_KEY:-}"
test -n "${e2b_api_key//[[:space:]]/}" || {
  printf 'E2B_API_KEY is missing from this host process. See ENVIRONMENT.md.\n' >&2
  exit 1
}

printf 'E2B_API_KEY: present (value not displayed)\n'
if test -n "${GREPTILE_API_KEY:-}"; then
  printf 'GREPTILE_API_KEY: present\n'
else
  printf 'GREPTILE_API_KEY: not set; optional\n'
fi
github_login=$(gh api user --jq .login) || {
  printf 'GitHub CLI authentication failed. Run gh auth login.\n' >&2
  exit 1
}
test -n "$github_login" || {
  printf 'GitHub CLI authentication failed. Run gh auth login.\n' >&2
  exit 1
}
printf 'GitHub CLI: authenticated as %s (token not displayed)\n' "$github_login"
uv run "$(dirname "$0")/tools/e2b_pr_sandbox.py" doctor
printf 'PR review package preflight: PASS\n'
