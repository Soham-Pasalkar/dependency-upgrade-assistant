"""Resolution API (FR-29, FR-30, FR-32; M12 criteria 10, 11 API part, 14)."""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.resolution.policy import COMPATIBILITY_DISCLOSURE
from src.resolution.resolver_runtime import RunResult
from tests.resolution_api_helpers import PACKAGE_JSON, approve, make_plan
from tests.resolution_doubles import install_service, wait_for_job


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def start(client, plan):
    return client.post(f"/api/proposals/{plan['proposal_id']}/resolve")


def test_resolve_job_status_and_view(client):
    install_service()
    plan = make_plan(client)
    resp = start(client, plan)
    assert resp.status_code == 202
    job = resp.json()["job"]
    assert job["status"] in ("QUEUED", "RUNNING", "SUCCEEDED")
    wait_for_job(job["id"])
    status = client.get(f"/api/jobs/{job['id']}").json()
    assert status["status"] == "SUCCEEDED" and status["started_at"] and status["deadline_at"]
    view = client.get(f"/api/proposals/{plan['proposal_id']}/resolution").json()
    assert view["status"] == "COMPATIBLE" and view["approval_eligible"] is True
    assert view["disclosure"] == COMPATIBILITY_DISCLOSURE
    assert view["proposed"]["role"] == "proposed" and view["original"]["role"] == "original"
    assert view["comparison"]["available"] is True
    packages = view["proposed"]["packages"]
    assert packages and all({"name", "version", "direct", "introduced", "upload", "filename"} <= set(p) for p in packages)
    assert all(p["upload"]["filename"] == p["filename"] for p in packages)


def test_duplicate_submission_is_409(client):
    import threading
    gate = threading.Event()
    install_service(lambda role, manifest: gate.wait(5) and None)
    plan = make_plan(client)
    first = start(client, plan)
    second = start(client, plan)
    gate.set()
    assert first.status_code == 202 and second.status_code == 409
    assert second.json()["detail"]["code"] == "job_active"
    wait_for_job(first.json()["job"]["id"])


def test_cancel_endpoint(client):
    import threading
    gate = threading.Event()

    def blocked(role, manifest):
        gate.wait(5)
        return RunResult("cancelled", None, b"", "", None, "x")
    install_service(blocked)
    plan = make_plan(client)
    job_id = start(client, plan).json()["job"]["id"]
    cancelled = client.post(f"/api/jobs/{job_id}/cancel").json()
    assert cancelled["cancel_requested"] is True or cancelled["status"] == "CANCELLED"
    gate.set()
    assert wait_for_job(job_id).status.value == "CANCELLED"
    view = client.get(f"/api/proposals/{plan['proposal_id']}/resolution").json()
    assert view["status"] == "UNKNOWN" and view["approval_eligible"] is False and view["proposed"] is None


def test_npm_plan_answers_immediately_as_unsupported(client):
    install_service()
    plan = make_plan(client, content=PACKAGE_JSON, manifest_type="package.json")
    resp = start(client, plan)
    assert resp.status_code == 200 and resp.json()["job"] is None
    view = resp.json()["resolution"]
    assert view["status"] == "UNSUPPORTED" and view["approval_eligible"] is False
    assert "npm compatibility verification is not yet supported" in view["proposed"]["unsupported"][0]["message"]


@pytest.mark.parametrize("path", ["/api/jobs/nope", "/api/proposals/prop_missing/resolution",
                                  "/api/jobs/..%2Fetc/cancel", "/api/proposals/a%20b/resolution"])
def test_unknown_or_invalid_ids(client, path):
    resp = client.post(path) if path.endswith("/cancel") else client.get(path)
    assert resp.status_code == 404


def test_resolve_unknown_plan(client):
    assert client.post("/api/proposals/prop_missing/resolve").status_code == 404


def test_request_protection_applies(client):
    plan = make_plan(client)
    with TestClient(app, headers={}) as bare:
        resp = bare.post(f"/api/proposals/{plan['proposal_id']}/resolve")
    assert resp.status_code == 403


def test_view_never_shows_compatible_before_evidence(client):
    import threading
    gate = threading.Event()
    install_service(lambda role, manifest: gate.wait(5) and None)
    plan = make_plan(client)
    job_id = start(client, plan).json()["job"]["id"]
    view = client.get(f"/api/proposals/{plan['proposal_id']}/resolution").json()
    assert view["status"] == "UNKNOWN" and view["approval_eligible"] is False and view["job"]["id"] == job_id
    gate.set()
    wait_for_job(job_id)


# -- export (FR-32d) -------------------------------------------------------------------------


def _introducing_pair(client):
    """Proposed set gains a package the original resolution did not have (via Requires-Dist)."""
    service, pypi, runtime = install_service()
    real_report = runtime.report

    def report(manifest):
        raw = json.loads(real_report(manifest))
        pin = next((line.split("==")[1] for line in manifest.requirements if line.startswith("requests==")), None)
        if pin and pin != "2.28.0":  # the proposed manifest
            dep = pypi.add("charset-normalizer", "3.0.0")
            entry = pypi.files["requests"][pin]
            version = pin
            meta_url = entry["url"] + ".metadata"
            meta = pypi.metadata[meta_url] + b"Requires-Dist: charset-normalizer>=2\n"
            pypi.metadata[meta_url] = meta
            import hashlib
            entry["core-metadata"] = {"sha256": hashlib.sha256(meta).hexdigest()}
            raw["install"].append({"download_info": {"url": dep["url"], "archive_info": {"hashes": {
                "sha256": dep["hashes"]["sha256"]}}}, "is_direct": False, "is_yanked": False, "requested": False,
                "metadata": {"name": "charset-normalizer", "version": "3.0.0"}})
            assert version
        return json.dumps(raw).encode()
    runtime.report = report
    plan = make_plan(client)
    wait_for_job(start(client, plan).json()["job"]["id"])
    return plan


def test_export_contains_matching_markdown_and_json(client):
    plan = _introducing_pair(client)
    view = client.get(f"/api/proposals/{plan['proposal_id']}/resolution").json()
    assert view["status"] == "COMPATIBLE", view
    assert view["comparison"]["introduced"] == ["charset-normalizer"]
    assert approve(client, plan).status_code == 200
    export = client.post("/api/export", json={"proposal_id": plan["proposal_id"]}).json()
    zip_item = next(i for i in export["items"] if i["artifact_type"] == "bundle_zip")
    archive = zipfile.ZipFile(io.BytesIO(client.get(zip_item["download_url"]).content))
    summary = json.loads(archive.read("summary.json"))
    report = archive.read("compatibility-report.md").decode()
    comp = summary["compatibility"]
    assert comp["status"] == "COMPATIBLE" and comp["disclosure"] == COMPATIBILITY_DISCLOSURE
    assert comp["proposed_fingerprint"] == view["proposed"]["fingerprint"]
    assert comp["original_fingerprint"] == view["original"]["fingerprint"]
    assert comp["comparison"]["introduced"] == ["charset-normalizer"]
    assert COMPATIBILITY_DISCLOSURE in report and comp["proposed_fingerprint"] in report
    for row in comp["proposed_packages"]:  # every package in the JSON is in the Markdown, with its date
        assert f"| {row['name']} | {row['version']} |" in report and row["upload_date"] in report
    introduced = next(r for r in comp["proposed_packages"] if r["name"] == "charset-normalizer")
    assert introduced["introduced"] is True and introduced["direct"] is False
    assert "| charset-normalizer | 3.0.0 | transitive | yes |" in report
    assert "### Original resolved dependencies" in report and plan["unified_diff"].splitlines()[0] in report
    checksums = archive.read("checksums.sha256").decode()
    assert "compatibility-report.md" in checksums
    assert "safe to install" not in report.lower() and "malware-free" not in report.lower()
