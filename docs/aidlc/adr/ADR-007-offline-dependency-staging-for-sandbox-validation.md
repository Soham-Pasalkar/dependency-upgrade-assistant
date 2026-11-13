# ADR-007: Offline dependency staging for sandbox validation

- Status: proposed
- Date: 2026-11-11

## Context
The sandbox runs with `--network none` (ADR-002), so upgraded dependencies cannot be installed inside it, and `dependency_staging.py` is a stub returning `NOT_REQUIRED`. The validation route does not pass the repository workspace, so validation is syntax-only (N5). Docker is unavailable locally; GitHub Actions provides live Docker evidence.

## Options considered
1. Enable network inside the container for installs.
2. Stage artifacts on the host from allow-listed registries without building or executing them, then install inside the sandbox with no index.
3. Keep syntax-only validation and label it honestly.

## Decision
Option 2 where verifiable, with option 3's honest labelling as the floor. Python: `pip download --only-binary=:all: --no-deps`-style resolution is avoided on the host because it can execute build backends; instead wheel files are fetched directly from PyPI JSON/Simple URLs with SHA-256 verification against registry digests. npm: tarballs are fetched from registry `dist.tarball` URLs and verified against `dist.integrity`; installs run in the container with `--offline --ignore-scripts`. Downloads are capped at 300 MB. Network isolation is never relaxed. Any staging path not verified by a test and a live Docker run is reported as a limitation.

## Consequences
Validation becomes meaningful for packages with wheels/tarballs; sdist-only Python packages report "not stageable" rather than building on the host. Transitive dependency resolution is limited to what lockfiles or pinned manifests specify; unresolved transitive dependencies surface as install failures, reported truthfully.
