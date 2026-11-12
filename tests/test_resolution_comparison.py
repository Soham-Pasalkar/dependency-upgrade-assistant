"""FR-32b introduced-package comparison (ADR-011); M12 criterion 14(b) unit part.

Uses the real Flask 2.0.3 -> 3.0.3 resolutions: Flask 3 adds a dependency on blinker. The comparison is
informational: these tests check it never changes status or fingerprint. Approval enforcement is P7.
"""
from __future__ import annotations

import pytest

from src.resolution.comparison import EvidenceBindingError, apply_comparison, compare
from src.resolution.fingerprint import ResolutionEnvironment, resolution_environment
from src.resolution.models import ResolutionRole, ResolutionStatus
from src.resolution.verifier import build_report, failed_report, verify_success
from tests.resolution_fixtures import ENV, client, context, deadline, manifest_text, report_bytes

ORIGINAL = manifest_text("flask_original")
PROPOSED = manifest_text("flask_proposed")


def ctxs(env=ENV, original_env=None, original_text=ORIGINAL, proposal_id="prop_1", **kw):
    original = context(original_text, role=ResolutionRole.ORIGINAL, env=original_env or env, proposal_id=proposal_id,
                       **kw)
    proposed = context(PROPOSED, original=original_text, env=env, proposal_id=proposal_id, **kw)
    return original, proposed


def verified(ctx, run):
    report = verify_success(ctx, report_bytes(run), client(), deadline())
    assert report.status == ResolutionStatus.COMPATIBLE, report.failure
    return report


@pytest.fixture
def pair():
    o_ctx, p_ctx = ctxs()
    return o_ctx, p_ctx, verified(o_ctx, "flask_original"), verified(p_ctx, "flask_proposed")


def test_both_compatible_introduces_exactly_the_new_packages(pair):
    o_ctx, p_ctx, original, proposed = pair
    comparison = compare(original, proposed, o_ctx, p_ctx)
    assert comparison.available and comparison.reason is None
    assert comparison.introduced == ["blinker"]


def test_apply_marks_packages_and_changes_nothing_else(pair):
    o_ctx, p_ctx, original, proposed = pair
    annotated = apply_comparison(proposed, compare(original, proposed, o_ctx, p_ctx))
    flags = {p.name: p.introduced for p in annotated.packages}
    assert flags["blinker"] is True  # introduced transitively
    assert flags["click"] is False and flags["Werkzeug"] is False and flags["Flask"] is False  # already present
    assert annotated.status == proposed.status and annotated.fingerprint == proposed.fingerprint
    assert [(p.name, p.direct, p.upload) for p in annotated.packages] == \
           [(p.name, p.direct, p.upload) for p in proposed.packages]
    assert all(p.upload.available for p in annotated.packages)


def test_original_is_the_complete_resolved_set_not_declared_entries(pair):
    o_ctx, p_ctx, original, proposed = pair
    # Declared entries of the original manifest: only Flask. Transitive packages present in the original
    # resolution (click, Werkzeug, ...) must not count as introduced.
    assert set(compare(original, proposed, o_ctx, p_ctx).introduced) == {"blinker"}


@pytest.mark.parametrize("status,reason", [
    (ResolutionStatus.CONFLICT, "original_conflict"),
    (ResolutionStatus.UNSUPPORTED, "original_unsupported"),
    (ResolutionStatus.RESOLUTION_FAILED, "original_resolution_failed"),
    (ResolutionStatus.RESOLVER_UNAVAILABLE, "original_resolver_unavailable"),
])
def test_original_not_compatible_makes_comparison_unavailable(pair, status, reason):
    o_ctx, p_ctx, _, proposed = pair
    original = build_report(o_ctx, status, failure="x")
    comparison = compare(original, proposed, o_ctx, p_ctx)
    assert (comparison.available, comparison.reason, comparison.introduced) == (False, reason, [])
    annotated = apply_comparison(proposed, comparison)
    assert all(p.introduced is None for p in annotated.packages)
    assert annotated.status == ResolutionStatus.COMPATIBLE  # the proposed result is unaffected


@pytest.mark.parametrize("status,reason", [
    (ResolutionStatus.CONFLICT, "proposed_conflict"),
    (ResolutionStatus.UNSUPPORTED, "proposed_unsupported"),
    (ResolutionStatus.RESOLUTION_FAILED, "proposed_resolution_failed"),
])
def test_proposed_not_compatible_makes_comparison_unavailable(pair, status, reason):
    o_ctx, p_ctx, original, _ = pair
    proposed = build_report(p_ctx, status, failure="x")
    comparison = compare(original, proposed, o_ctx, p_ctx)
    assert (comparison.available, comparison.reason) == (False, reason)
    assert apply_comparison(proposed, comparison).status == status  # never upgraded


def test_missing_original_evidence(pair):
    o_ctx, p_ctx, _, proposed = pair
    assert compare(None, proposed, o_ctx, p_ctx).reason == "original_missing"


@pytest.mark.parametrize("change", [
    dict(env=resolution_environment("arm64")),  # different target architecture
    dict(env=ResolutionEnvironment(**{**ENV.__dict__, "pip_args": ENV.pip_args + ("--pre",)})),  # resolver config
    dict(env=ResolutionEnvironment(**{**ENV.__dict__, "allowlist_version": "egress-allowlist/2"})),
])
def test_different_configuration_makes_comparison_unavailable(pair, change):
    _, p_ctx, _, proposed = pair
    other_o_ctx = context(ORIGINAL, role=ResolutionRole.ORIGINAL, **change)
    other_original = build_report(other_o_ctx, ResolutionStatus.COMPATIBLE, packages=proposed.packages[:1])
    assert compare(other_original, proposed, other_o_ctx, p_ctx).reason == "environment_mismatch"


def test_different_constraints_make_comparison_unavailable(pair, tmp_path):
    _, p_ctx, _, proposed = pair
    (tmp_path / "c.txt").write_text("Werkzeug<4\n")
    o_ctx = context(ORIGINAL + "-c c.txt\n", role=ResolutionRole.ORIGINAL, workspace_dir=str(tmp_path),
                    manifest_path="requirements.txt")
    original = build_report(o_ctx, ResolutionStatus.COMPATIBLE, packages=proposed.packages[:1])
    assert compare(original, proposed, o_ctx, p_ctx).reason == "environment_mismatch"


def test_original_evidence_from_another_context_is_not_used(pair):
    o_ctx, p_ctx, original, proposed = pair
    other_ctx = context(ORIGINAL, role=ResolutionRole.ORIGINAL, proposal_id="prop_other")
    assert compare(original, proposed, other_ctx, p_ctx).reason == "evidence_mismatch"
    # Proposed evidence replayed as the original baseline:
    assert compare(proposed, proposed, o_ctx, p_ctx).reason == "evidence_mismatch"


def test_proposed_evidence_must_belong_to_the_proposed_context(pair):
    o_ctx, _, original, proposed = pair
    with pytest.raises(EvidenceBindingError):
        compare(original, proposed, o_ctx, context(PROPOSED, original=ORIGINAL, proposal_id="prop_other"))
    with pytest.raises(EvidenceBindingError):
        compare(original, proposed.model_copy(update={"fingerprint": "0" * 64}), o_ctx, ctxs()[1])


def test_contexts_must_have_the_right_roles(pair):
    o_ctx, p_ctx, original, proposed = pair
    with pytest.raises(ValueError):
        compare(original, proposed, p_ctx, p_ctx)


def test_duplicate_package_names_make_comparison_unavailable(pair):
    o_ctx, p_ctx, original, proposed = pair
    duplicated = original.model_copy(update={"packages": original.packages + [
        original.packages[0].model_copy(update={"name": original.packages[0].name.upper()})]})
    assert compare(duplicated, proposed, o_ctx, p_ctx).reason == "invalid_evidence"


def test_empty_resolved_set_makes_comparison_unavailable(pair):
    o_ctx, p_ctx, original, proposed = pair
    empty = original.model_copy(update={"packages": []})
    assert compare(empty, proposed, o_ctx, p_ctx).reason == "invalid_evidence"


def test_failed_original_evidence_is_still_separate_evidence(pair):
    o_ctx, p_ctx, _, proposed = pair
    original = failed_report(o_ctx, "verification_failed", "x")
    assert original.fingerprint == o_ctx.fingerprint() != proposed.fingerprint
    assert original.role == ResolutionRole.ORIGINAL and proposed.role == ResolutionRole.PROPOSED
