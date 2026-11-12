# ADR-009: Isolated binary-only resolver execution

- Status: proposed (pending G2)
- Date: 2026-11-12
- Requirements: FR-22, FR-23, FR-24, FR-29, NFR-13. Threats: T-30, T-31, T-38.

## Context
Compatibility must be decided by pip's resolver (FR-22). Resolving a source distribution means running its build backend, which is arbitrary, untrusted code. Code that runs alongside the resolver could tamper with the evidence the resolver produces (T-31). The M11 validation sandbox installs offline from prebuilt wheels only, so a source-only package can't be validated either.

Measured on 2026-11-12 (local pip probes, not M12 tests):
- `pip install --dry-run --only-binary=:all: --report -` writes the report to stdout.
- pip read wheel metadata from PEP 658 `.metadata` files: 10 fetches, 0 full wheel downloads for a 5-package set.
- Source-only requirements fail distinctly: `(from versions: none)`, for example `pycrypto==2.6.1`.
- Conflicts print their causes, for example `scipy 1.10.0 depends on numpy<1.27.0 and >=1.19.5`.

## Options considered
1. **pip on the host, binary-only.** No code execution, but the pip process and index responses are exposed to the host network and filesystem. No isolation boundary.
2. **pip in one container with source builds allowed.** Build code runs next to the resolver and its report, so it could forge or overwrite the report (T-31). Rejected.
3. **Two-stage source builds.**
   - *Design:* a per-sdist build container produces a wheel. The wheel is validated and handed to a fresh resolver container running `--find-links` and `--only-binary`. The supervisor then verifies.
   - *Strengths:*
     - the authoritative resolver runs no third-party code;
     - each build can only affect its own package's metadata, which its author controls anyway;
     - build output can be checked structurally: zip structure, size and entry caps, a single `.dist-info`, `Name` and `Version` matching the requested sdist, hash recorded.
   - *Weaknesses:*
     - pip decides which sdists to build during resolution, so the build set isn't known in advance. That means iterative resolve, build, re-resolve rounds with bounded but non-trivial ordering.
     - Validating build output means parsing untrusted archives.
     - The result still can't be validated end to end, because staging is wheels-only.
   - *Verdict:* not trustworthy within M12's risk budget. Deferred beyond M13; it needs separate approval.
4. **pip in a disposable container, binary-only, with no third-party code at all; source-only requirements UNSUPPORTED.**

## Decision
Option 4.
- **Image:** the pinned validation image (`python:3.11-slim` by digest), so the target runtime and platform match validation.
- **Command:** `python -m pip install --dry-run --ignore-installed --only-binary=:all: --isolated --no-input --disable-pip-version-check --index-url https://pypi.org/simple --proxy http://<proxy-ip>:3128 --report - -r /in/requirements.txt [-c /in/constraints-N.txt]`.
- **Container settings:**
  - non-root, read-only root, `--cap-drop=ALL`, `--security-opt no-new-privileges`, `--init`;
  - NFR-13 memory, CPU, process and tmpfs limits;
  - a tmpfs for pip's caches and temporary files;
  - the input directory mounted read-only, and no writable host mount;
  - DNS disabled (`--dns 127.0.0.1`);
  - no credentials, Docker socket or application environment;
  - removed after the run.
- **One container per run:** each job makes two runs, original then proposed (FR-29, FR-32b). Each runs in its own fresh container with identical settings. Nothing is shared between the two except the job's proxy and network (ADR-010).
- **Source-only requirements:** detected by the supervisor from pip's `(from versions: none)` failure, cross-checked against PyPI's PEP 691 file list. They are UNSUPPORTED (`source_build_required`), never CONFLICT.
- **Conflicts involving source-only releases:** a conflict that involves packages with source-only releases satisfying their constraints is UNSUPPORTED, because a wheel-only search can't prove incompatibility.
- **Run deadline:** the container is killed when the run's deadline is reached (ADR-012).

## Consequences
- **Gained:** no build backend, `setup.py` or package code runs during resolution, so T-31 has no code-execution path by design. This is to be proven by M12 criterion 4. Evidence integrity rests on the pinned pip image and on supervisor verification (ADR-011).
- **Lost:** packages and versions that ship only as sdists can't be COMPATIBLE in M12. This matches validation, which is also wheels-only.
- **Requires Docker:** without it, resolution is RESOLVER_UNAVAILABLE and approval is blocked.
- **Revisit:** source-build resolution, as a separate future milestone.
