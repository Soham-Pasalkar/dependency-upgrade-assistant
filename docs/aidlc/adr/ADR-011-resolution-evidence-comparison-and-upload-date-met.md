# ADR-011: Resolution evidence, comparison and upload-date metadata

- Status: proposed (pending G2)
- Date: 2026-11-12
- Requirements: FR-23, FR-26, FR-27, FR-28, FR-31, FR-32, NFR-14. Threats: T-31, T-32, T-33, T-35, T-38.

## Context
Approval, validation, apply and export must rely only on evidence that belongs to the exact proposal and environment. That evidence must not be forgeable by clients or by anything inside the resolver. FR-32 adds three things to every resolution:
- a second, original-manifest resolution that serves as the review baseline;
- the upload date of each selected file;
- a disclosure that compatibility is not a security verdict.

These additions inform review. They must not weaken or replace the approval evidence.

## Options considered
1. Trust the resolver's report as evidence.
2. Store a boolean "resolved" flag.
3. Treat resolver output as an untrusted proposal. The trusted supervisor verifies it independently, computes the fingerprints, derives the comparison and the dates from PyPI's own metadata, and stores everything.

For the comparison baseline specifically:
- **(a)** declared manifest entries: rejected by the owner, because every transitive package would look new;
- **(b)** the original manifest's complete resolved set (chosen).

## Decision
Option 3, with baseline (b).

**Roles.** Every job produces at most two evidence records in `dua_resolutions`, distinguished by `role`:
- `original`: the original manifest. Used only as the FR-32b baseline. Informational.
- `proposed`: the proposed manifest. This is the **authoritative** evidence for FR-28.

Both roles use identical settings: the same image digest, target (CPython 3.11, Linux, the host architecture), pip arguments, prerelease and yanked policy, constraints files and allow-list version. Each runs in its own container (ADR-009) within the same job (ADR-012).

**Verification** (supervisor, in the application process, for each role):
1. Read stdout with a cap of `RESOLVER_MAX_REPORT_MB`, and require pip report schema version 1.
2. For each selected install item, check that the name and version are valid, the artifact is a wheel on `files.pythonhosted.org`, and a SHA-256 is present.
3. Fetch the package's PEP 691 JSON simple index (`Accept: application/vnd.pypi.simple.v1+json`) through the hardened registry client. It must be within `MAX_LARGE_RESPONSE_BYTES` (16 MB; numpy measured 2.8 MB on 2026-11-12).
4. Find the selected file by exact filename and check that its SHA-256 equals the index's. A mismatch or a missing file is `verification_failed`.
5. Fetch that wheel's PEP 658 `.metadata`, checked against the index-advertised metadata hash. If it is unavailable, use the per-version JSON API with a `release_level_metadata` warning. Never download or open archives, and never run code.
6. Check that all direct requirements and constraints are satisfied by the selected set.
7. Check that every selected package's `Requires-Dist` is satisfied within the set, evaluated with the target-environment markers and the requested extras.
8. Check that every `Requires-Python` admits the target, and that there are no extra or unrelated packages.

Any failure gives RESOLUTION_FAILED (`verification_failed`, with details).

**Fingerprints.** Each role's fingerprint is the SHA-256 of canonical JSON (sorted keys, no whitespace), computed only by the supervisor.
- **Common elements:**
  - `policy_version` (`dua-resolution/1`), `ecosystem`;
  - the resolver image digest, Docker host architecture and target (`cp311`, `linux`);
  - pip arguments: index, `--only-binary=:all:`, `--isolated`, prerelease and yanked policy;
  - `egress-allowlist/1`;
  - the proposal ID;
  - constraints file paths and SHA-256s.
- **`original` adds:** `role: original` and the original manifest SHA-256.
- **`proposed` adds:** `role: proposed`, the unified-diff SHA-256, and the original and proposed manifest SHA-256s.

Resource limits and timeouts are excluded: they don't change a verified result, only whether a run finishes. Upload dates and the comparison are outputs, not inputs, so they are excluded too. Any input change produces a new proposed fingerprint and invalidates earlier evidence (FR-27c).

**Upload dates (FR-32c), a deterministic rule per selected file:**
1. Identify the selected file by the filename in pip's `download_info.url` and the SHA-256 in its `archive_info.hashes`.
2. **Source A, preferred:** the entry in the PEP 691 JSON simple index already fetched in step 3 whose `filename` and `hashes.sha256` both match. Use its PEP 700 `upload-time` if it is present and parses as an RFC 3339 UTC timestamp.
3. **Source B, fallback:** `https://pypi.org/pypi/<name>/<version>/json`, the `urls[]` entry whose `filename` and `digests.sha256` both match. Use its `upload_time_iso_8601`.
4. Otherwise the value is `date unavailable`, with a reason: `file_not_matched`, `field_missing`, `unparseable` or `fetch_failed`.
5. **Recorded:** the value, the source (`pep700-simple-json` or `pypi-json-api`), the endpoint URL and the retrieval time (UTC).

Other files of the same version are never consulted, so neither the earliest nor the latest file is used. Project pages, cache timestamps and estimates are never used. Dates are display data only and never affect status, fingerprint or approval.

**Comparison (FR-32b).**
- **When it is computed:** only when both role records exist for the same job, both are COMPATIBLE, and their common fingerprint elements are identical.
- **What it is:** introduced packages are the names in the proposed resolved set that are absent from the original resolved set. Names are normalized per PEP 503.
- **Otherwise:** the comparison is `comparison unavailable`, with a reason: `original_<status>` (for example `original_conflict`), `original_missing` (the run did not complete) or `environment_mismatch`. No package is labelled introduced.

**Authoritative versus informational.**
- **Authoritative:** the `proposed` record's status and fingerprint drive enforcement.
- **Informational:** the `original` record, the comparison, the upload dates and the FR-32a disclosure.

The comparison never changes approval, for three reasons:
1. Approval asks whether the proposed set itself resolves (FR-28). A broken original is often the reason for upgrading, so gating on it would block exactly the upgrades that fix conflicts.
2. A comparison-based gate would add a soft, judgement-based approval path, and the owner rejected acknowledgement overrides.
3. The owner's G1 approval (event 299) states that the comparison is informational and adds no approval gate.

**Enforcement.** Recompute the proposed fingerprint from the stored proposal and current configuration. Require that the latest `proposed` row for the proposal has an equal fingerprint and status COMPATIBLE. The approval binds `resolution_fingerprint`. Validation, apply and export re-check it together with the M11 validation-policy fingerprint. Client-supplied statuses, fingerprints, dates or comparison fields are ignored.

**Disclosure (FR-32a).** The UI's compatibility panel and the Markdown export render a fixed server-provided text whenever the status is COMPATIBLE. The text says the complete set resolves; it does not mean the packages are vetted or safe; and DUA does not perform malware detection.

## Consequences
- **Evidence trust:** tampered resolver output fails verification unless it matches PyPI's own metadata.
- **Cost:** about one index fetch and one metadata fetch per selected package per role. That is up to twice the earlier estimate (500 packages per role by default), and up to 2.8 MB per large-package index. The registry cache is reused across the two roles.
- **Registry drift:** a release published between the two runs can change the comparison. Both records carry timestamps, the UI shows them, and evidence expires with its plan (NFR-14).
- **Not a security verdict:** these checks prove resolvability against PyPI's metadata, not trustworthiness (T-35).
