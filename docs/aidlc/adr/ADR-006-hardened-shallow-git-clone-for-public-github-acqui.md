# ADR-006: Hardened shallow git clone for public GitHub acquisition

- Status: proposed
- Date: 2026-11-11

## Context
FR-1R needs real repository contents. The legacy `git_service.py` performs `git clone --depth 1` with a GitHub URL regex and is tested, but it passes a PAT via an environment variable (contrary to ADR-003) and lacks resource limits, config isolation and network controls. The owner chose hardened git clone over the tarball API.

## Options considered
1. Expose legacy `git_service` directly.
2. Download the GitHub tarball through the REST API and extract it.
3. A new `RepositoryService` that reuses `git_service` URL validation and workspace helpers, adding hardening.

## Decision
Option 3. Before cloning: strict URL parse to canonical `https://github.com/<owner>/<repo>.git`, then `api.github.com/repos/<owner>/<repo>` metadata check (exists, public, `size` ≤ 200 MB), with resolved-IP validation and no cross-host redirects. Clone with `GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, isolated `HOME`, `GIT_TERMINAL_PROMPT=0`, `-c core.symlinks=false -c protocol.allow=never -c protocol.https.allow=always -c filter.lfs.smudge= -c filter.lfs.process= -c filter.lfs.required=false`, `--depth 1 --single-branch --no-recurse-submodules --no-tags`, 90 s timeout. During cloning, resource use is bounded rather than only checked afterwards: the git process runs in its own process group with `RLIMIT_FSIZE` set to the per-file cap, and a watchdog polls workspace size and file count every 250 ms and kills the group as soon as the 500 MB quota or 20,000-file limit is exceeded; the 90 s timeout also kills the group. After cloning, a final measurement is taken and the workspace is deleted on any violation. No PAT path is exposed (private repos deferred).

## Consequences
Reuses tested code and avoids archive-extraction risk. Requires `git` on the host. Shallow clones still transfer full blobs of HEAD. Without a size-limited filesystem (unavailable without root on macOS), the quota is a tightly bounded soft limit: overshoot is at most about one polling interval of transfer and never more than the per-file cap. This is documented as a limitation rather than claimed as a hard quota. Revisit for private repositories (ADR-003 `GIT_ASKPASS` flow).
