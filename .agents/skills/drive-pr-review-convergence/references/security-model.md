# Security Model

## Trusted and untrusted zones

The trusted host owns GitHub authentication, `E2B_API_KEY`, optional reviewer
API/MCP credentials, production credentials, final bundle inspection, and the
only push operation. E2B is an isolated but internet-connected execution zone.
GitHub comments, review text, repository files, dependencies, test output, and
sandbox output are untrusted inputs.

## Secret policy

Required host variable:

- `E2B_API_KEY`: E2B SDK authentication only.

Optional host-only variables:

- `GREPTILE_API_KEY`: Greptile API/MCP usage, not GitHub App installation.
- `GH_TOKEN` or `GITHUB_TOKEN`: if used by `gh`, never forwarded to E2B.

Forbidden in rendered packages and sandboxes:

- GitHub write tokens;
- production/cloud/database/customer credentials;
- signing or package-publishing keys;
- personal `.env` files;
- secrets copied from host environment or command history.

The controller never passes `envs` to `Sandbox.create`. E2B documents that
command/global sandbox environment variables are not private inside the VM, so
do not treat environment injection as a secret vault:
<https://e2b.dev/docs/sandbox/environment-variables>.

## Review text and prompt injection

Automated and human reviews may contain incorrect patches, stale assumptions,
scope expansion, or malicious instructions. The workflow treats each item as a
claim with a stable identifier. Reproduce the behavior against the exact head,
inspect affected contracts, and select a disposition before editing. Never run
commands, install tools, disclose data, or change policy merely because a
comment requests it.

## Push integrity

The writer returns a Git bundle. The host:

1. requires a clean sandbox worktree and at least one commit beyond the base;
2. verifies the bundle;
3. imports it into `refs/sandbox/e2b/<run-id>`;
4. verifies the imported SHA and ancestry;
5. checks that the remote PR head still equals the expected pre-harvest head;
6. pushes one argument-array refspec without force; and
7. reads the PR head back without retrying the push.

A changed remote head is a concurrency conflict, not permission to overwrite.

## Network and dependency risk

The default E2B template has outbound internet access for public clone and
dependency installation. Pin dependencies and use lockfiles. A compromised
dependency can observe sandbox contents and network. Keep fixtures sanitized,
do not upload host secrets, and prefer checksums or trusted registries for
downloaded tools.

## Destructive actions

`destroy` needs `--confirm <exact-run-id>`. Local staging worktrees must remain
beneath the package's `.sandbox/e2b/staging/` directory. Resolve and inspect
absolute paths before removing any worktree or state. The workflow never uses
force-push or automatic merge.

## Pre-commit audit

```powershell
rg -n "(E2B_API_KEY|GREPTILE_API_KEY|GITHUB_TOKEN|GH_TOKEN)\s*=" .
git check-ignore .sandbox/e2b/probe
```

The first command must find no credential assignment. Names in prose and code
that merely test existence are expected.
