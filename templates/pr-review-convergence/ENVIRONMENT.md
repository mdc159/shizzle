# Environment, Credentials, and Support Boundary

## Required host software

- PowerShell 7 on Windows, or Bash on Linux/WSL.
- Git.
- GitHub CLI (`gh`), authenticated to read the target pull request and, only
  when explicitly authorized, push its branch.
- uv. The shipped scripts declare Python 3.11+ and pin `e2b==2.35.0`.
- An E2B account and API key with enough quota to build a template and create
  the selected sandboxes.

Run `bootstrap.ps1` or `bootstrap.sh` to verify the live environment. The checks
report only whether a credential exists; they never print its value.

## Exact API-key location

Create or copy the key from the E2B dashboard, then expose it to the trusted
host process as the environment variable `E2B_API_KEY`. The E2B SDK officially
uses this variable by default: <https://e2b.dev/docs/api-key>.

On Mike's native Windows/Codex Desktop setup, use the Windows **User environment
variables** UI for `E2B_API_KEY`, then completely restart Codex Desktop so the
new process inherits it. Verify existence without revealing the value:

```powershell
Test-Path Env:E2B_API_KEY
```

For a temporary PowerShell session, assign it interactively without recording
the value in a repository, shell-history command, task prompt, or log. On
Linux/WSL/CI, inject it from that environment's secret manager and verify only:

```bash
test -n "${E2B_API_KEY:-}"
```

Do not put the key in `goal.md`, `review-policy.json`, `setup.sh`, a committed
`.env`, a command-line argument, a GitHub comment, or E2B sandbox environment.
`E2B_ACCESS_TOKEN` is a separate CLI credential and is not required by these
SDK-based tools.

## Other credentials

- `gh auth login` or an existing host GitHub CLI session provides GitHub access.
  The controller does not upload the GitHub token to E2B.
- `GREPTILE_API_KEY` is optional host-only API/MCP access. GitHub PR review also
  requires the Greptile GitHub App and repository indexing; an API key alone is
  not that installation.
- CodeRabbit and Cubic GitHub App reviews require no key in this package.
- Production, cloud-provider, database, signing, package-registry, and customer
  credentials stay outside the sandbox. Tests needing them require a separately
  approved, least-privilege adapter and sanitized fixtures.

The controller clones public repositories over credential-free HTTPS, pins the
exact PR head, and returns writer work as a verified Git bundle. A private
repository will fail to clone by design. Supporting it requires a reviewed host
bundle/upload flow or a short-lived read-only credential strategy; neither is
silently inferred by this package.

## E2B execution surface

The supplied template is Linux with Node.js 22, npm, Git, uv, Bash, jq, rsync,
shellcheck, curl, and SSH client tools. `setup.sh` installs the target project's
locked dependencies. The template tiers are:

| Tier | CPU | Memory | Intended use |
| --- | ---: | ---: | --- |
| `small` | 2 | 4 GiB | lightweight audit or lint reader |
| `standard` | 4 | 8 GiB | default writer or normal test lane |
| `heavy` | 8 | 16 GiB | memory/CPU-heavy validation |

Sandbox count affects cost and synchronization. Start with one writer. Add a
reader only when it shortens the critical path or gives genuinely independent
evidence. There is no universal E2B image for GPU, nested container, native
Windows/macOS, real-device, private-network, or GUI-only validation.

## Secret and state audit

Before committing the package:

```powershell
git check-ignore .sandbox/e2b/probe
rg -n "(E2B_API_KEY|GREPTILE_API_KEY|GITHUB_TOKEN|GH_TOKEN)=" .
```

The first command should identify an ignore rule. The second should return no
assignment containing a credential value. Environment-variable names in this
documentation are expected and safe.
