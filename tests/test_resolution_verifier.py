"""Supervisor verification of untrusted resolver output (FR-27; ADR-011).

M12 criterion 5 (forgery matrix on captured real reports) and the unit parts of criteria 1-4.
All data is recorded (tests/fixtures/resolution); FakePyPI serves it through the real client.
These tests do not exercise Docker, the egress proxy or a live resolver.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from src.resolution import python_resolver
from src.resolution.fingerprint import ResolutionEnvironment, resolution_environment
from src.resolution.metadata_client import Deadline, DeadlineExceeded, MetadataFetchError
from src.resolution.models import ResolutionRole, ResolutionStatus, UnsupportedReason
from src.resolution.policy import COMPATIBILITY_DISCLOSURE
from src.resolution.python_resolver import RunOutcome, evaluate_run
from src.resolution.verifier import (
    build_report,
    check_binding,
    unsupported_report,
    verify_success,
)
from tests.resolution_fixtures import (
    CAPTURE,
    ENV,
    FakePyPI,
    client,
    clone,
    context,
    deadline,
    item,
    manifest_text,
    report_bytes,
    report_json,
    stderr,
)

FLASK = manifest_text("flask_proposed")


def verify(run="flask_proposed", content=None, mutate=None, fake=None, **kw):
    return verify_success(context(content or manifest_text(run)), report_bytes(run, mutate), client(fake), deadline(),
                          **kw)


def failure_code(report):
    assert report.status == ResolutionStatus.RESOLUTION_FAILED, report.failure
    assert report.packages == [] and report.disclosure is None
    return report.failure.split(":", 1)[0]


# -- accepted real reports -------------------------------------------------------------------


@pytest.mark.parametrize("run", ["flask_original", "flask_proposed", "requests_socks", "numpy_scipy_pandas"])
def test_real_reports_are_verified_compatible(run):
    ctx = context(manifest_text(run))
    report = verify_success(ctx, report_bytes(run), client(), deadline())
    assert report.status == ResolutionStatus.COMPATIBLE, report.failure
    assert report.fingerprint == ctx.fingerprint() and report.inputs == ctx.inputs()
    assert report.role == ResolutionRole.PROPOSED and report.image == ENV.image
    assert report.disclosure == COMPATIBILITY_DISCLOSURE
    assert report.resolver == "pip 26.2.2"
    recorded = {i["metadata"]["name"] for i in report_json(run)["install"]}
    assert {p.name for p in report.packages} == recorded
    assert all(p.upload.available and p.upload.filename == p.filename for p in report.packages)


def test_report_contains_every_fr27b_field():
    ctx = context(FLASK)
    report = verify_success(ctx, report_bytes("flask_proposed"), client(), deadline(), now=1000.0)
    assert report.status == ResolutionStatus.COMPATIBLE and report.ecosystem == "python"
    assert report.resolver == "pip 26.2.2" and report.image == ENV.image
    assert report.target == {"python": "3.11.18", "implementation": "cp311", "platform": "linux",
                             "machine": "x86_64"}
    assert report.allowlist_version == "egress-allowlist/1" and report.policy_version == "dua-resolution/1"
    assert set(report.inputs) == {"proposal_id", "original_sha256", "proposed_sha256", "diff_sha256",
                                  "constraints", "common_digest"}
    assert report.created_at == 1000.0 and report.fingerprint == ctx.fingerprint()
    assert report.conflicts == [] and report.unsupported == [] and report.warnings == []
    packages = {p.name: p for p in report.packages}
    assert packages["Flask"].constraints == ["==3.0.3"]
    assert ">=3.0.0" in packages["Werkzeug"].constraints and ">=1.6.2" in packages["blinker"].constraints
    assert all(p.version and p.filename and p.sha256 and p.upload.filename == p.filename
               for p in report.packages)


def test_direct_and_transitive_packages_are_distinguished():
    report = verify()
    direct = {p.name for p in report.packages if p.direct}
    assert direct == {"Flask"}
    assert {"blinker", "Werkzeug", "click"} <= {p.name for p in report.packages if not p.direct}


def test_extras_dependencies_are_required_and_recorded():
    report = verify("requests_socks")
    names = {p.name: p for p in report.packages}
    assert names["requests"].requested_extras == ["socks"]
    assert "PySocks" in names and not names["PySocks"].direct
    # Without the extra, PySocks is unrelated to the input.
    no_extra = verify("requests_socks", content="requests==2.32.3\n")
    assert failure_code(no_extra) in ("extras_mismatch", "extra_packages")


def test_marker_excluded_dependency_is_absent():
    metadata = (FakePyPI().body("https://files.pythonhosted.org/x/flask-3.0.3-py3-none-any.whl.metadata")
                or b"").decode()
    assert "Requires-Dist: importlib-metadata>=3.6.0; python_version < '3.10'" in metadata
    report = verify()
    assert report.status == ResolutionStatus.COMPATIBLE
    assert "importlib-metadata" not in {p.name.lower() for p in report.packages}
    # A report that includes the marker-excluded package is rejected as unrelated.
    forged = verify(mutate=lambda d: d["install"].append({**clone(item(d, "blinker")), "metadata": {
        "name": "importlib-metadata", "version": "8.0.0"}}))
    assert failure_code(forged) in ("report_contradictory", "extra_packages")


def test_hash_mode_requires_the_selected_file():
    data = report_json("flask_proposed")
    sha = item(data, "flask")["download_info"]["archive_info"]["hashes"]["sha256"]
    ok = verify(content=f"Flask==3.0.3 --hash=sha256:{sha}\n")
    assert ok.status == ResolutionStatus.COMPATIBLE
    bad = verify(content=f"Flask==3.0.3 --hash=sha256:{'0' * 64}\n")
    assert failure_code(bad) == "hash_mismatch"


def test_repository_constraints_are_enforced(tmp_path):
    (tmp_path / "constraints.txt").write_text("Werkzeug<3\n")
    ctx = context(FLASK + "-c constraints.txt\n", workspace_dir=str(tmp_path), manifest_path="requirements.txt")
    report = verify_success(ctx, report_bytes("flask_proposed"), client(), deadline())
    assert failure_code(report) == "constraint_violated"


# -- forgery matrix: resolver-side output cannot manufacture COMPATIBLE --------------------------


def _drop(name):
    return lambda d: d["install"].remove(item(d, name))


def _set_sha(name, sha):
    def edit(d):
        info = item(d, name)["download_info"]["archive_info"]
        info["hashes"]["sha256"] = sha
        info["hash"] = f"sha256={sha}"
    return edit


def _set_url(name, url):
    return lambda d: item(d, name)["download_info"].__setitem__("url", url)


def _numpy_210(d):
    entry = next(f for f in FakePyPI().index("numpy")["files"]
                 if f["filename"] == "numpy-2.1.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl")
    numpy = item(d, "numpy")
    numpy["metadata"]["version"] = "2.1.0"
    numpy["download_info"] = {"url": entry["url"], "archive_info": {
        "hashes": {"sha256": entry["hashes"]["sha256"]}, "hash": f"sha256={entry['hashes']['sha256']}"}}
    for name in ("pandas", "python-dateutil", "pytz", "tzdata"):
        d["install"].remove(item(d, name))


FORGERIES = {
    "status_field_injected": (lambda d: d.__setitem__("status", "COMPATIBLE"), "report_schema"),
    "fingerprint_field_injected": (lambda d: d.__setitem__("fingerprint", "f" * 64), "report_schema"),
    "role_field_injected": (lambda d: d.__setitem__("role", "original"), "report_schema"),
    "item_field_injected": (lambda d: item(d, "flask").__setitem__("introduced", False), "report_schema"),
    "report_version_changed": (lambda d: d.__setitem__("version", "2"), "report_schema"),
    "missing_top_level_field": (lambda d: d.pop("pip_version"), "report_schema"),
    "missing_package_record": (_drop("werkzeug"), "incomplete"),
    "extra_unrelated_package": (lambda d: d["install"].append(item(report_json("numpy_scipy_pandas"), "pytz")),
                                "extra_packages"),
    "duplicate_normalized_name": (lambda d: d["install"].append(
        {**clone(item(d, "flask")), "metadata": {**item(d, "flask")["metadata"], "name": "FLASK"}}),
        "report_duplicate_package"),
    "invalid_version": (lambda d: item(d, "flask")["metadata"].__setitem__("version", "three"),
                        "report_invalid_version"),
    "version_contradicts_wheel": (lambda d: item(d, "flask")["metadata"].__setitem__("version", "3.0.2"),
                                  "report_contradictory"),
    "wrong_hash": (_set_sha("werkzeug", "ab" * 32), "hash_mismatch"),
    "uppercase_hash": (_set_sha("werkzeug", "AB" * 32), "report_missing_hash"),
    "hash_fields_disagree": (lambda d: item(d, "flask")["download_info"]["archive_info"].__setitem__(
        "hash", "sha256=" + "cd" * 32), "report_contradictory"),
    "non_allowlisted_host": (lambda d: item(d, "flask")["download_info"].__setitem__(
        "url", item(d, "flask")["download_info"]["url"].replace("files.pythonhosted.org", "evil.example")),
        "report_artifact_host"),
    "plain_http": (lambda d: item(d, "flask")["download_info"].__setitem__(
        "url", item(d, "flask")["download_info"]["url"].replace("https://", "http://")), "report_artifact_host"),
    "credentials_in_url": (lambda d: item(d, "flask")["download_info"].__setitem__(
        "url", item(d, "flask")["download_info"]["url"].replace("https://", "https://u:p@")), "report_artifact_host"),
    "same_file_other_path": (lambda d: item(d, "flask")["download_info"].__setitem__(
        "url", "https://files.pythonhosted.org/packages/00/00/flask-3.0.3-py3-none-any.whl"), "url_mismatch"),
    "sdist_selected": (lambda d: item(d, "flask")["download_info"].__setitem__(
        "url", "https://files.pythonhosted.org/packages/00/flask-3.0.3.tar.gz"), "report_not_a_wheel"),
    "incompatible_platform_wheel": (lambda d: item(d, "markupsafe")["download_info"].__setitem__(
        "url", "https://files.pythonhosted.org/packages/00/markupsafe-3.0.4-cp311-cp311-win_amd64.whl"),
        "incompatible_wheel"),
    "direct_url_install": (lambda d: item(d, "flask").__setitem__("is_direct", True), "report_direct_reference"),
    "vcs_info": (lambda d: item(d, "flask")["download_info"].__setitem__("vcs_info", {"vcs": "git"}),
                 "report_schema"),
    "transitive_claimed_requested": (lambda d: item(d, "werkzeug").__setitem__("requested", True),
                                     "requested_mismatch"),
    "direct_claimed_unrequested": (lambda d: item(d, "flask").__setitem__("requested", False),
                                   "requested_mismatch"),
    "extras_forged": (lambda d: item(d, "flask").__setitem__("requested_extras", ["async"]), "extras_mismatch"),
    "foreign_environment": (lambda d: d["environment"].__setitem__("sys_platform", "win32"),
                            "environment_mismatch"),
    "wrong_python": (lambda d: d["environment"].__setitem__("python_full_version", "3.12.1"),
                     "environment_mismatch"),
    "empty_install": (lambda d: d.__setitem__("install", []), "report_empty_resolution"),
    "install_not_list": (lambda d: d.__setitem__("install", {}), "report_schema"),
    "item_not_object": (lambda d: d["install"].append("flask"), "report_schema"),
    "too_deep": (lambda d: item(d, "flask")["metadata"].__setitem__("x", json.loads("[" * 40 + "]" * 40)),
                 "report_too_deep"),
}


@pytest.mark.parametrize("name", sorted(FORGERIES))
def test_forged_flask_reports_fail_closed(name):
    mutate, expected = FORGERIES[name]
    assert failure_code(verify(mutate=mutate)) == expected


def test_claimed_resolution_of_a_real_conflict_is_rejected():
    """A resolver claiming success for numpy==2.1.0 with scipy==1.10.0 is refuted by scipy's own metadata."""
    report = verify("numpy_scipy_pandas", content="numpy==2.1.0\nscipy==1.10.0\n", mutate=_numpy_210)
    assert failure_code(report) == "unsatisfied"
    assert "numpy 2.1.0 does not satisfy" in report.failure and "scipy 1.10.0" in report.failure


def test_unsatisfied_direct_requirement_is_rejected():
    assert failure_code(verify(content="Flask==2.0.3\n")) == "unsatisfied"


@pytest.mark.parametrize("raw,code", [
    (b"", "report_empty"),
    (b"   \n", "report_empty"),
    (b"{not json", "report_malformed"),
    (b"\xff\xfe", "report_malformed"),
    (b"[]", "report_malformed"),
    (b'{"version": "1", "version": "1"}', "report_malformed"),
])
def test_malformed_stdout_fails_closed(raw, code):
    report = verify_success(context(FLASK), raw, client(), deadline())
    assert failure_code(report) == code


def test_oversized_report_is_rejected_before_parsing():
    raw = report_bytes("flask_proposed")
    report = verify_success(context(FLASK), raw, client(), deadline(), max_report_bytes=len(raw) - 1)
    assert failure_code(report) == "report_too_large"


def test_package_count_limit():
    report = verify_success(context(FLASK), report_bytes("flask_proposed"), client(), deadline(), max_packages=3)
    assert failure_code(report) == "report_too_many_packages"


def test_report_cannot_set_status_or_fingerprint():
    """Whatever the resolver prints, identity and status come from the supervisor's context."""
    ctx = context(FLASK, proposal_id="prop_trusted")
    report = verify_success(ctx, report_bytes("flask_proposed"), client(), deadline())
    assert report.fingerprint == ctx.fingerprint() and report.inputs["proposal_id"] == "prop_trusted"


# -- verification against PyPI's own data ----------------------------------------------------


def test_file_missing_from_index_is_rejected():
    fake = FakePyPI()
    data = fake.index("werkzeug")
    data["files"] = []
    fake.set_index("werkzeug", data)
    assert failure_code(verify(fake=fake)) == "file_not_in_index"


def test_duplicate_index_entries_are_ambiguous():
    fake = FakePyPI()
    data = fake.index("blinker")
    data["files"] = data["files"] + data["files"]
    fake.set_index("blinker", data)
    assert failure_code(verify(fake=fake)) == "ambiguous_index_entry"


def test_metadata_hash_mismatch_fails():
    fake = FakePyPI()
    fake.edit_index_file("blinker", "blinker-1.9.0-py3-none-any.whl",
                         lambda e: e.__setitem__("core-metadata", {"sha256": "0" * 64}))
    report = verify(fake=fake)
    assert failure_code(report) == "fetch_failed" and "hash_mismatch" in report.failure


def test_metadata_naming_another_version_fails():
    fake = FakePyPI()
    entry = next(f for f in fake.index("blinker")["files"] if f["filename"].endswith(".whl"))
    body = fake.body(entry["url"] + ".metadata").replace(b"Version: 1.9.0", b"Version: 1.8.0")
    fake.overrides[entry["url"] + ".metadata"] = (200, body)
    fake.edit_index_file("blinker", entry["filename"],
                         lambda e: e.__setitem__("core-metadata", {"sha256": hashlib.sha256(body).hexdigest()}))
    assert failure_code(verify(fake=fake)) == "metadata_mismatch"


def test_json_api_fallback_when_no_pep658_metadata():
    fake = FakePyPI()
    fake.edit_index_file("blinker", "blinker-1.9.0-py3-none-any.whl", lambda e: (
        e.pop("core-metadata", None), e.pop("data-dist-info-metadata", None)))
    report = verify(fake=fake)
    assert report.status == ResolutionStatus.COMPATIBLE
    assert report.warnings == ["release_level_metadata: blinker 1.9.0"]


def test_index_unavailable_fails_closed():
    fake = FakePyPI()
    fake.overrides["https://pypi.org/simple/werkzeug/"] = (503, b"")
    report = verify(fake=fake)
    assert failure_code(report) == "fetch_failed"


def test_index_requires_python_excluding_target_fails():
    fake = FakePyPI()
    fake.edit_index_file("blinker", "blinker-1.9.0-py3-none-any.whl", lambda e: e.__setitem__("requires-python", ">=3.12"))
    assert failure_code(verify(fake=fake)) == "requires_python"


def test_yanked_transitive_release_is_not_permitted():
    fake = FakePyPI()
    fake.edit_index_file("blinker", "blinker-1.9.0-py3-none-any.whl", lambda e: e.__setitem__("yanked", "broken"))
    assert failure_code(verify(fake=fake)) == "yanked_not_permitted"


def test_yanked_exact_pin_resolves_with_warning():
    fake = FakePyPI()
    fake.edit_index_file("flask", "flask-3.0.3-py3-none-any.whl", lambda e: e.__setitem__("yanked", "regression"))
    report = verify(fake=fake)
    assert report.status == ResolutionStatus.COMPATIBLE
    assert report.warnings == ["yanked: flask==3.0.3 (regression)"]


def test_resolver_claim_of_not_yanked_is_ignored():
    fake = FakePyPI()
    fake.edit_index_file("blinker", "blinker-1.9.0-py3-none-any.whl", lambda e: e.__setitem__("yanked", True))
    assert failure_code(verify(fake=fake, mutate=lambda d: item(d, "blinker").__setitem__("is_yanked", False))) == \
        "yanked_not_permitted"


# -- synthetic project: policies that recorded data does not exercise ---------------------------
# "demo-pkg" is invented for these tests; its index and metadata are built here, not recorded.


def _demo(fake, versions, requires_dist=(), selected="2.0.0rc1"):
    files = []
    for version in versions:
        filename = f"demo_pkg-{version}-py3-none-any.whl"
        url = f"https://files.pythonhosted.org/packages/de/mo/{filename}"
        meta = ("Metadata-Version: 2.1\nName: demo-pkg\nVersion: " + version + "\n"
                + "".join(f"Requires-Dist: {r}\n" for r in requires_dist)).encode()
        fake.overrides[url + ".metadata"] = (200, meta)
        sha = hashlib.sha256(filename.encode()).hexdigest()
        files.append({"filename": filename, "url": url, "hashes": {"sha256": sha}, "requires-python": None,
                      "yanked": False, "core-metadata": {"sha256": hashlib.sha256(meta).hexdigest()},
                      "upload-time": "2026-01-01T00:00:00.000000Z"})
    fake.set_index("demo-pkg", {"meta": {"api-version": "1.4"}, "name": "demo-pkg", "files": files})
    chosen = next(f for f in files if selected in f["filename"])
    data = report_json("flask_proposed")
    data["install"] = [{"download_info": {"url": chosen["url"], "archive_info": {"hashes": {
        "sha256": chosen["hashes"]["sha256"]}}}, "is_direct": False, "is_yanked": False, "requested": True,
        "metadata": {"name": "demo-pkg", "version": selected}}]
    return json.dumps(data).encode()


@pytest.mark.parametrize("requirement,versions,status", [
    ("demo-pkg>=1", ["1.0.0", "2.0.0rc1"], ResolutionStatus.RESOLUTION_FAILED),  # final exists: prerelease not allowed
    ("demo-pkg==2.0.0rc1", ["1.0.0", "2.0.0rc1"], ResolutionStatus.COMPATIBLE),  # explicitly pinned prerelease
    ("demo-pkg>=1.5", ["1.0.0", "2.0.0rc1"], ResolutionStatus.COMPATIBLE),  # no final release satisfies
])
def test_prerelease_policy(requirement, versions, status):
    fake = FakePyPI()
    raw = _demo(fake, versions)
    report = verify_success(context(requirement + "\n"), raw, client(fake), deadline())
    assert report.status == status, report.failure
    if status == ResolutionStatus.RESOLUTION_FAILED:
        assert failure_code(report) == "prerelease_not_permitted"


def test_marker_depending_on_host_kernel_is_unverifiable():
    fake = FakePyPI()
    raw = _demo(fake, ["2.0.0rc1"], requires_dist=['blinker; platform_release >= "5"'])
    report = verify_success(context("demo-pkg==2.0.0rc1\n"), raw, client(fake), deadline())
    assert failure_code(report) == "unverifiable_marker"


def test_direct_url_dependency_in_metadata_is_rejected():
    fake = FakePyPI()
    raw = _demo(fake, ["2.0.0rc1"], requires_dist=["blinker @ https://example.com/blinker.whl"])
    assert failure_code(verify_success(context("demo-pkg==2.0.0rc1\n"), raw, client(fake), deadline())) == \
        "direct_reference"


# -- evidence binding --------------------------------------------------------------------------


def _verified(**kw):
    ctx = context(FLASK, **kw)
    return ctx, verify_success(ctx, report_bytes("flask_proposed"), client(), deadline())


def test_binding_accepts_own_context():
    ctx, report = _verified()
    assert check_binding(report, ctx) is None


@pytest.mark.parametrize("other,reason", [
    (dict(proposal_id="prop_other"), "inputs_mismatch"),
    (dict(role=ResolutionRole.ORIGINAL), "role_mismatch"),
    (dict(diff="--- other\n"), "inputs_mismatch"),
    (dict(original="Flask==2.0.3\n"), "inputs_mismatch"),
    (dict(env=resolution_environment("arm64")), "target_mismatch"),
])
def test_binding_rejects_evidence_from_another_context(other, reason):
    _, report = _verified()
    assert check_binding(report, context(FLASK, **other)) == reason


@pytest.mark.parametrize("field,value,reason", [
    ("policy_version", "dua-resolution/0", "policy_mismatch"),
    ("image", "python:3.11-slim@sha256:" + "9" * 64, "image_mismatch"),
    ("allowlist_version", "egress-allowlist/0", "allowlist_mismatch"),
    ("fingerprint", "0" * 64, "fingerprint_mismatch"),
])
def test_binding_rejects_altered_evidence(field, value, reason):
    ctx, report = _verified()
    assert check_binding(report.model_copy(update={field: value}), ctx) == reason


def test_binding_rejects_changed_pip_policy():
    _, report = _verified()
    env = ResolutionEnvironment(**{**ENV.__dict__, "pip_args": ENV.pip_args + ("--pre",)})
    assert check_binding(report, context(FLASK, env=env)) == "inputs_mismatch"


def test_proposed_evidence_cannot_be_replayed_as_original():
    _, report = _verified()
    original_ctx = context(FLASK, role=ResolutionRole.ORIGINAL)
    assert check_binding(report, original_ctx) == "role_mismatch"
    relabelled = report.model_copy(update={"role": ResolutionRole.ORIGINAL})
    assert check_binding(relabelled, original_ctx) in ("inputs_mismatch", "fingerprint_mismatch")


# -- run outcomes: only exit 0 + verified report can be COMPATIBLE -----------------------------------


def run(content, exit_code, stdout=b"", log="", fake=None):
    return evaluate_run(context(content), RunOutcome(exit_code, stdout, log), client(fake), deadline())


def test_successful_run_is_verified():
    assert run(FLASK, 0, report_bytes("flask_proposed")).status == ResolutionStatus.COMPATIBLE


def test_exit_zero_without_report_fails():
    assert failure_code(run(FLASK, 0)) == "report_empty"


def test_failed_run_with_a_report_is_contradictory():
    assert failure_code(run(FLASK, 1, report_bytes("flask_proposed"))) == "contradictory_output"


@pytest.mark.parametrize("log", [
    "", "ERROR: something", "Successfully resolved\nCOMPATIBLE", stderr("flask_proposed"),
    "The conflict is caused by:\n    nonsense line\n", json.dumps({"status": "COMPATIBLE"}),
])
def test_failed_runs_are_never_compatible(log):
    report = run(FLASK, 1, b"", log)
    assert report.status in (ResolutionStatus.CONFLICT, ResolutionStatus.UNSUPPORTED,
                             ResolutionStatus.RESOLUTION_FAILED)
    assert report.disclosure is None


def test_invalid_status_for_exit_code_is_a_hard_error(monkeypatch):
    monkeypatch.setattr(python_resolver, "verify_success",
                        lambda ctx, *a, **k: build_report(ctx, ResolutionStatus.CONFLICT))
    with pytest.raises(RuntimeError):
        run(FLASK, 0, b"{}")


def test_unsupported_input_is_never_resolved():
    with pytest.raises(ValueError):
        run("-e .\n", 0, report_bytes("flask_proposed"))
    report = unsupported_report(context("-e .\nFlask\n"))
    assert report.status == ResolutionStatus.UNSUPPORTED
    assert [u.reason for u in report.unsupported] == [UnsupportedReason.EDITABLE]


def test_deadline_is_not_turned_into_evidence():
    with pytest.raises(DeadlineExceeded):
        verify_success(context(FLASK), report_bytes("flask_proposed"), client(),
                       Deadline(at=0.0, clock=lambda: 1.0))


# -- failure classification from real pip logs ---------------------------------------------------


def real(run_name, fake=None):
    return run(manifest_text(run_name), CAPTURE["runs"][run_name]["exit_code"], b"", stderr(run_name), fake)


def test_real_conflict_is_conflict_with_pip_causes():
    report = real("conflict_numpy_scipy")
    assert report.status == ResolutionStatus.CONFLICT
    assert report.conflicts[0].causes == ["The user requested numpy==2.1.0",
                                          "scipy 1.10.0 depends on numpy<1.27.0 and >=1.19.5"]
    assert report.conflicts[0].summary.startswith("Cannot install numpy==2.1.0 and scipy==1.10.0")


def test_conflict_involving_source_only_release_is_unsupported():
    fake = FakePyPI()
    data = fake.index("scipy")
    data["files"] = [f for f in data["files"] if not f["filename"].endswith(".whl")]  # 1.10.0 becomes sdist-only
    fake.set_index("scipy", data)
    report = real("conflict_numpy_scipy", fake)
    assert report.status == ResolutionStatus.UNSUPPORTED
    assert report.unsupported[0].reason == UnsupportedReason.SOURCE_ONLY_CONFLICT


def test_real_sdist_only_requirement_is_unsupported():
    report = real("sdist_only")
    assert report.status == ResolutionStatus.UNSUPPORTED
    assert [u.reason for u in report.unsupported] == [UnsupportedReason.SOURCE_BUILD_REQUIRED]


def test_real_requires_python_conflict_is_conflict():
    report = real("requires_python")
    assert report.status == ResolutionStatus.CONFLICT
    assert report.conflicts[0].causes == ["django requires Python >=3.12"]


def test_unknown_project_is_resolution_failed():
    report = run("not-a-real-project==1.0\n", 1, b"", "ERROR: Could not find a version that satisfies the "
                 "requirement not-a-real-project==1.0 (from versions: none)\n")
    assert failure_code(report) == "no_matching_distribution"


def test_unparseable_conflict_is_resolution_failed():
    assert failure_code(run(FLASK, 1, b"", "ERROR: ResolutionImpossible: for help visit ...\n")) == \
        "unparsed_conflict"


def test_classification_fetch_failure_fails_closed():
    fake = FakePyPI()
    fake.errors["https://pypi.org/simple/scipy/"] = MetadataFetchError("timeout", "slow")
    assert failure_code(real("conflict_numpy_scipy", fake)) == "fetch_failed"


def test_resolver_log_text_is_sanitized_and_bounded():
    log = "ERROR: Cannot install a and b because these package versions have conflicting dependencies.\n" \
          "The conflict is caused by:\n" + "".join(f"    The user requested a=={i}\x1b[31m\n" for i in range(80))
    report = run("a==1\nb==1\n", 1, b"", log, FakePyPI())
    causes = report.conflicts[0].causes if report.conflicts else []
    assert len(causes) <= 50 and all("\x1b" not in c for c in causes)
