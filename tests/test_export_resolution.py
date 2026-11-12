"""FR-32d export: Markdown and JSON agree on the compatibility evidence (M12 criterion 14d)."""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.resolution.policy import COMPATIBILITY_DISCLOSURE
from tests.resolution_api_helpers import approve, make_plan
from tests.resolution_doubles import install_service, wait_for_job


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def start(client, plan):
    return client.post(f"/api/proposals/{plan['proposal_id']}/resolve")

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
