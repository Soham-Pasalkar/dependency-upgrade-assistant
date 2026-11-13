"""M11 / F10: version data origin is explicit end to end; offline data is never 'verified' or planned."""
from __future__ import annotations

from unittest.mock import MagicMock

import requests
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import manifest as manifest_routes
from src.models.manifest import RegistryDataSource, RegistryLookupStatus
from src.models.plan import UpgradePlanRequest
from src.models.recommendation import ValidationStatus
from src.services.manifest_service import ManifestService
from src.services.registry_client import NpmRegistryClient, PyPIRegistryClient, RegistryPackageMetadata
from src.services.upgrade_planner import UpgradePlannerService


def _resp(status, data):
    import json
    r = MagicMock()
    r.status_code = status
    r.headers = {}
    body = json.dumps(data).encode()
    r.iter_content.return_value = [body]
    return r


def test_live_answer_then_cache_hit_are_labelled():
    s = MagicMock(spec=requests.Session)
    s.get.return_value = _resp(200, {"info": {"version": "2.0.0"}, "releases": {"1.0.0": [], "2.0.0": []}})
    c = PyPIRegistryClient(session=s, cache_ttl=100)
    assert c.get_package_metadata("pkg").source == "LIVE_REGISTRY"
    assert c.get_package_metadata("pkg").source == "CACHED_REGISTRY"


def test_registry_failure_without_catalog_entry_is_unavailable():
    s = MagicMock(spec=requests.Session)
    s.get.side_effect = requests.ConnectionError("down")
    meta = PyPIRegistryClient(session=s).get_package_metadata("not-in-catalog-xyz")
    assert meta.status == RegistryLookupStatus.NETWORK_ERROR and meta.source == "UNAVAILABLE"
    npm = MagicMock(spec=requests.Session)
    npm.get.side_effect = requests.ConnectionError("down")
    assert NpmRegistryClient(session=npm).get_package_metadata("x").source == "UNAVAILABLE"


def test_not_found_is_a_live_answer():
    s = MagicMock(spec=requests.Session)
    s.get.return_value = _resp(404, {})
    meta = PyPIRegistryClient(session=s).get_package_metadata("nope")
    assert meta.status == RegistryLookupStatus.NOT_FOUND and meta.source == "LIVE_REGISTRY"


class _OfflinePyPI:
    def get_package_metadata(self, name):
        return RegistryPackageMetadata(
            package_name=name, latest_version="2.32.3", available_versions=["2.28.0", "2.32.3"],
            status=RegistryLookupStatus.OFFLINE_CATALOG, error_message="PyPI unavailable; using the bundled offline catalog",
            source="OFFLINE_CATALOG",
        )


def test_offline_data_is_labelled_in_analysis_recommendations_and_never_planned():
    from src.services.recommendation_service import RecommendationService
    from src.models.recommendation import RecommendationBatchRequest

    analysis = ManifestService(pypi_client=_OfflinePyPI()).analyze_manifest("requirements.txt", "requests==2.28.0\n")
    dep = analysis.dependencies[0]
    assert dep.registry_status == RegistryLookupStatus.OFFLINE_CATALOG
    assert dep.data_source == RegistryDataSource.OFFLINE_CATALOG
    assert "offline catalog" in (dep.diagnostic_message or "")

    recs = RecommendationService().generate_recommendations(
        RecommendationBatchRequest(manifest_analysis=analysis, force_deterministic=True)
    ).recommendations
    rec = recs[0]
    assert rec.data_source == "OFFLINE_CATALOG"
    assert rec.validation_status == ValidationStatus.UNVERIFIED_OFFLINE  # never VALIDATED / rule-verified
    assert "offline catalog" in rec.explanation

    plan = UpgradePlannerService().create_upgrade_proposal(UpgradePlanRequest(
        manifest_type="requirements.txt", original_content="requests==2.28.0\n", recommendations=recs,
    ))
    assert plan.total_planned == 0
    assert "requests==2.32.3" not in plan.unified_diff
    assert any("offline catalog" in i.rationale for i in plan.plan_items)


def test_api_exposes_data_source(monkeypatch):
    monkeypatch.setattr(manifest_routes.manifest_service, "pypi_client", _OfflinePyPI())
    client = TestClient(app)
    run = client.post("/api/runs/manifest", json={"manifest_type": "requirements.txt", "content": "requests==2.28.0\n"}).json()
    dep = run["manifests"][0]["analysis"]["dependencies"][0]
    assert dep["data_source"] == "OFFLINE_CATALOG" and dep["registry_status"] == "OFFLINE_CATALOG"
    recs = client.post(f"/api/runs/{run['run_id']}/manifests/0/recommendations", json={"force_deterministic": True}).json()
    assert recs["recommendations"][0]["data_source"] == "OFFLINE_CATALOG"
    assert recs["recommendations"][0]["validation_status"] == "UNVERIFIED_OFFLINE"
    plan = client.post(f"/api/runs/{run['run_id']}/manifests/0/plan", json={}).json()
    assert plan["total_planned"] == 0
