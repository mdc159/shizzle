# Portability and Limitations

## Support matrix

| Surface | Built in | Required adaptation |
| --- | --- | --- |
| Public GitHub repository | Yes | Deterministic `setup.sh` and validation commands |
| Same-repository PR branch | Yes | Host account must be allowed to push |
| Fork PR | Read/test yes | Default push rejects fork heads; use contributor workflow |
| Private repository | No | Reviewed adapter from `private-repositories.md` |
| Linux CPU build/test | Yes | Add locked project dependencies in setup |
| Standard Node/Python/Rust/Go tools | Usually | Add pinned toolchain commands or custom template |
| Docker Compose rendering | Often | Docker daemon/nested containers are not guaranteed |
| Privileged Docker/Kubernetes | No | Dedicated CI or compatible runner |
| GPU/CUDA | No | GPU runner or provider-specific lane |
| Windows/macOS-native behavior | No | Native runner/worktree |
| Browser/UI | Partial | Add Playwright/browser template or hosted browser CI |
| Mobile/real hardware | No | Device lab/manual lane |
| Private VPC/database/service | No | Approved network and secret design |
| Production deployment | No | Separate explicitly authorized release workflow |

## What self-contained means

After rendering, the package contains every script and document required to
execute its supported public-repository/Linux workflow. It does not mean that a
package rendered for one PR can be reused unchanged for another, or that one VM
image can emulate every deployment target.

Render again for each PR so the repository, head, checks, reviewers, setup,
validation, repair budget, and quiet window are explicit.

## Parallelism

One writer is sufficient. Add readers only if their work is independent and
long enough to overlap meaningfully. Typical useful layouts:

- writer only: small or normal PR;
- writer + audit: workflow, security, configuration, or migration change;
- writer + audit + test: multiple long independent suites.

Beyond three lanes, synchronization and compute usually outweigh wall-time
savings unless the repository has several genuinely independent test shards.

## Provider drift

E2B SDK behavior, resource tiers, maximum lifetimes, prices, reviewer schemas,
GitHub App permissions, and plan quotas are externally controlled and can
change. The scripts pin `e2b==2.35.0`, but documentation must be rechecked before
upgrading or promising current pricing/limits.

## Evidence limitations

Passing sandbox tests proves only the surfaces exercised there. Hosted CI,
deployments, migrations against real engines, hardware, browser behavior, and
external services require their own evidence. Report missing validation as a
residual risk rather than silently equating unit tests with production proof.
