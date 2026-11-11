# Completion report: Dependency Upgrade Assistant 2.0

<!-- Gate G5. Honesty section first: what is NOT verified matters more than what is. -->

## Summary
Milestones M1–M8 shipped a FastAPI backend and a React 19 frontend. Together
they cover manifest paste/upload analysis for `requirements.txt`,
`pyproject.toml` and `package.json`, deterministic PyPI and npm metadata
evaluation, recommendations with an Ollama advisory layer and a deterministic
fallback, unified-diff upgrade plans, a hardened Docker sandbox configuration,
and `.patch`/`.zip`/`.md` export. Each milestone passed G4, and the test suites
are green.

The delivered system does **not** meet the G1-approved intent as a product.
An engineering takeover on 2026-11-11 found these gaps:

- **Missing MUST requirements.**
  - FR-1 (repository acquisition) and FR-2 (recursive multi-manifest
    discovery) exist only in the legacy Streamlit app. The 2.0 API and UI do
    not expose them.
  - FR-5 (AST inspection), FR-7 (Monaco hunk review), FR-12 (SSE progress) and
    FR-13 (persistent history) are not delivered.
  - The G3 plan neither scheduled nor deferred any of these. This is recorded
    as deviation note N4.
- **Trust-boundary defects** (risk note N5):
  - The server accepts pipeline state asserted by the client, including
    `validation_status`.
  - Approval is a client-side boolean.
  - Export is not gated on approval.
- **Validation is not meaningful.** The validation route never passes the
  repository workspace to the sandbox, and dependency staging is a stub. In
  practice, validation proves manifest syntax only.

Historical test results show the code behaves as its tests specify. They do
**not** show the product is ready to release.

## Verification performed
All checks below were run in this session on 2026-11-11, on branch
`feature/dependency-upgrade-assistant-2.0` at HEAD `5cd2800`.

| Check | Command | Result |
|-------|---------|--------|
| Full regression (backend) | `python3 -m pytest -q -o addopts="" -p no:cacheprovider` | 564 passed, 6 skipped (Docker-dependent), 0 failed, 7 warnings |
| Frontend tests | `npx vitest run` (in `frontend/`) | 6 files, 28 passed, 0 failed |
| Dependency audit (npm) | `npm audit` and `npm audit --omit=dev` (in `frontend/`) | 0 vulnerabilities |
| Dependency audit (Python) | `pip-audit` | Not run: tool not installed, and no installs were authorized |
| Secrets scan (working tree) | `scan_secrets.py .` | PASS, 0 findings |
| Secrets scan (history) | `scan_secrets.py . --history` | 4 findings, all synthetic fixtures in `tests/test_code_inspector.py` (commit 4258719) that test the secret detector; not real credentials |
| Audit chain | `aidlc.py verify` | PASS, 0 errors, 0 warnings |

## Not verified
- **Live Docker execution.** Docker is unavailable locally, which is why
  6 tests were skipped. The M7 and M8 reports cite GitHub Actions runs
  42970027044 and 42972477402 with live Docker jobs. This audit did not
  re-check those runs. Even where they passed, they exercised syntax-only
  validation, because the route supplies no workspace and staging is a stub.
- **Live Ollama inference.** Only the deterministic fallback path was
  exercised.
- **Python dependency vulnerability audit.** `pip-audit` is not installed.
- **NFR-5 latency** (P95 < 200 ms). This was not measured.
- **NFR-6 accessibility** (WCAG 2.1 AA). There was no audit with assistive
  technology or a contrast tool.
- **Repository URL analysis end to end** through the 2.0 API. No such
  endpoint exists.
- **Large-metadata packages** (e.g. numpy). These fail because of the 2 MB
  registry response cap. Production-scale behaviour is unverified.

## Security review
**Verified by tests:**
- Sandbox container configuration: `--network none`, non-root `1000:1000`,
  `--cap-drop=ALL`, read-only root, pinned image digests, resource caps.
- Origin, Content-Type and `X-DUA-Client` checks on mutating requests.
- Secret redaction.
- Path-containment checks in the exporter and manifest handling.

**Open items:**
- NFR-1 / T-1: there is no Host-header validation, so the DNS-rebinding
  mitigation is unimplemented.
- `upgrade_planner` trusts the client-supplied `validation_status`.
- Approval is not enforced server-side. The UI never calls `/plan/apply`, and
  export works on unapproved proposals.
- The legacy `git_service` passes the PAT through the `DUA_GIT_TOKEN`
  environment variable rather than an in-memory `GIT_ASKPASS` pipe, contrary
  to ADR-003 and T-7. It is not reachable from the 2.0 API.
- Clone hardening, size and file-count limits, and SSRF controls for
  repository acquisition are not implemented. The 2.0 stack has no
  acquisition path yet.

## Performance
No NFR-5 measurements were taken. An in-memory metadata cache exists, but its
effect on latency is unmeasured. The registry client's 2 MB response cap
causes analysis of packages with large metadata to fail, which is a
correctness and reliability gap rather than a latency one.

## Documentation
- `README.md` at the repository root.
- `docs/SETUP_AND_DEMO_GUIDE.md`: local run commands, environment variables,
  demo walkthroughs.
- `docs/architecture/` and `docs/security/`.
- AI-DLC records:
  - `docs/aidlc/` (requirements, architecture, threat model, milestone plan,
    ADR-001 to ADR-004)
  - `docs/aidlc/reports/M1.md` to `M8.md`

The setup guide describes the delivered paste/upload workflow accurately. It
does not claim repository-URL analysis through the 2.0 UI.

## Residual risks
- **Client-asserted pipeline state and unenforced approval.** Owner: project
  owner. Follow-up: Inception round 2, trust-boundary milestone.
- **Syntax-only sandbox validation presented as validation.** Owner: project
  owner. Follow-up: round 2, Docker validation milestone (Python and npm
  offline staging, lockfiles, integrity checks).
- **Large registry responses fail.** Owner: project owner. Follow-up: round 2,
  registry reliability milestone.
- **FR-1 and FR-2 are absent from the 2.0 stack.** Owner: project owner.
  Follow-up: round 2, GitHub repository analysis milestone, with hardened
  clone, SSRF/DNS/redirect controls and resource limits.
- **Host-header validation is missing (T-1).** Owner: project owner.
  Follow-up: round 2, trust-boundary milestone.
- **Stale requirements text** (NFR-7 test count; npm listed out of scope).
  Owner: project owner. Follow-up: G1 Revision 2.

## Release recommendation
**do_not_ship.** Do not release v2.0 as a production product.

The codebase is a sound engineering baseline: the tests are green, the sandbox
hardening is good, and the deterministic analysis core works. But it fails
FR-1 and FR-2. It enforces no approval or trust boundary on the server. And
the validation it reports does not exercise the upgraded dependencies.

Recommendation: keep it as the baseline for Inception round 2 and address the
residual risks above under new G1–G3 approvals.
