# Private Repository Adapters

The default controller intentionally uses a credential-free HTTPS clone and
therefore fails closed for private repositories. Do not solve this by copying a
long-lived GitHub token into E2B.

## Preferred adapter: host-created source bundle

Design an adapter that:

1. uses authenticated host Git to fetch the exact PR head and base;
2. verifies the repository and PR identity with `gh api`;
3. creates a Git bundle containing only the required commits and refs;
4. scans the bundle source worktree for secrets and unrelated local changes;
5. uploads the bundle as bytes to the sandbox;
6. clones or fetches from that local bundle without network credentials;
7. verifies the resulting sandbox head equals the recorded GitHub head; and
8. keeps the existing verified-bundle return and head-checked host push path.

This preserves the one-way credential boundary: authenticated source retrieval
and final push occur on the host, while source execution occurs in E2B.

The controller implements this adapter as `create`/`fanout --source-bundle
<host-bundle>` plus `--source-repo-root <checkout>`:

- `provision` verifies the bundle contains the exact PR head SHA recorded by
  `gh api` before uploading anything;
- the bundle bytes are uploaded to the sandbox and cloned without network
  credentials, then the checked-out HEAD is re-verified against the recorded
  head;
- the bundle-backed clone records `setup.mode: "bundle"` in the run record;
- `--source-repo-root` records the trusted checkout used as the working
  directory for harvest bundle verification, import, and the head-checked
  push, because a rendered package directory is not a git repository.

Produce the bundle from the authenticated host checkout, for example:

```bash
git -C <trusted-checkout> bundle create pr-<n>.bundle <pr-head-ref>
```

## Alternative: short-lived read-only credential

Use only when the bundle approach cannot support required submodules or large
file storage. The design must specify:

- GitHub App installation token rather than a personal token;
- repository-scoped read-only permissions;
- short expiration and explicit revocation;
- redaction from command lines, Git remotes, process listings, logs, and files;
- no write permission;
- cleanup verification after clone; and
- security approval for the exposure.

The current project implements only the bundle adapter. The short-lived
read-only credential alternative remains unimplemented; that omission is a safety
boundary, not an unfinished automatic fallback.

## Submodules and Git LFS

Public repositories with private submodules or LFS objects are effectively
private for this workflow. Treat each dependency as a separate credential and
provenance decision. Do not allow Git credential helpers to persist a token in
the sandbox.
