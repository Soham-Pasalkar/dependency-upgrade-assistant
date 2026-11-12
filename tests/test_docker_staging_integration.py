"""Live Docker integration: real offline staging + real container install + real test run (FR-18, ADR-007).

These tests need network access (to stage packages from PyPI / npm on the host) and a running Docker
daemon. They are skipped when Docker is unavailable and run in the CI `docker-sandbox-validation` job.
Nothing here is mocked: a pass means the container actually installed the staged dependencies with
networking disabled and executed the repository's own tests.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.api.config import settings
from src.models.manifest import Ecosystem
from src.models.plan import PatchIntegrityResult, ProposalStatus, UpgradeProposalResponse
from src.models.sandbox import SandboxExecutionRequest
from src.sandbox.runner import DockerSandboxRunner
from src.services.proposal_registry import ProposalRegistry
from src.services.sandbox_service import SandboxService

pytestmark = pytest.mark.live_staging


def _require_docker() -> None:
    avail = DockerSandboxRunner().check_availability()
    if not avail.is_available:
        if os.environ.get("DUA_REQUIRE_DOCKER") == "1":
            pytest.fail(f"Docker is required in this job but unavailable: {avail.error_message}")
        pytest.skip(f"Live Docker daemon unavailable: {avail.error_message}")


def _proposal(manifest_type: str, ecosystem: Ecosystem, original: str, proposed: str) -> UpgradeProposalResponse:
    return UpgradeProposalResponse(
        manifest_type=manifest_type, ecosystem=ecosystem, file_path=manifest_type,
        original_content=original, proposed_content=proposed, unified_diff="",
        integrity=PatchIntegrityResult(is_valid=True, can_apply=True, can_reverse=True, syntax_valid=True,
                                       target_versions_match=True, unrelated_dependencies_unchanged=True,
                                       original_unchanged=True),
        status=ProposalStatus.READY_FOR_REVIEW,
    )


@pytest.fixture
def workspace_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    return settings.resolved_workspace_root


def _validate(workspace: Path, root: Path, proposal: UpgradeProposalResponse, timeout: int = 120):
    registry = ProposalRegistry()
    pid = registry.store(proposal)
    service = SandboxService(proposal_registry=registry)
    rel = str(workspace.resolve().relative_to(root))
    return service.execute_validation(
        SandboxExecutionRequest(proposal_id=pid, timeout_seconds=timeout), source_workspace=rel
    )


def _print_report(report) -> None:
    print("status:", report.status, "staging:", report.staging_status)
    for c in report.commands:
        print(f"  {c.command_name}: {c.status} exit={c.exit_code}")
    print("diagnostics:", report.diagnostics)
    print(report.logs[-3000:])


def test_python_offline_staging_install_and_tests(workspace_root):
    _require_docker()
    repo = workspace_root / "repos" / "py-fixture"
    (repo / "tests").mkdir(parents=True)
    original = "six==1.15.0\npytest==8.3.3\n"
    proposed = "six==1.16.0\npytest==8.3.3\n"
    (repo / "requirements.txt").write_text(original)
    (repo / "tests" / "test_smoke.py").write_text(
        "import six\n\n\ndef test_upgraded_dependency_is_importable():\n    assert six.__version__ == '1.16.0'\n"
    )
    report = _validate(repo, workspace_root, _proposal("requirements.txt", Ecosystem.PYTHON, original, proposed))
    _print_report(report)

    assert report.staging_status == "STAGED"
    names = {c.command_name: c for c in report.commands}
    assert names["dependency_install"].is_passed, "offline pip install inside the container failed"
    assert names["py_compile"].is_passed
    assert names["pytest"].is_passed, "the repository's tests did not pass against the staged upgrade"
    assert report.status.value == "PASSED"
    assert report.network_enabled is False


def test_python_staged_install_fails_honestly_when_tests_fail(workspace_root):
    _require_docker()
    repo = workspace_root / "repos" / "py-failing"
    (repo / "tests").mkdir(parents=True)
    content = "six==1.16.0\npytest==8.3.3\n"
    (repo / "requirements.txt").write_text(content)
    (repo / "tests" / "test_smoke.py").write_text(
        "import six\n\n\ndef test_expects_old_version():\n    assert six.__version__ == '1.15.0'\n"
    )
    report = _validate(repo, workspace_root, _proposal("requirements.txt", Ecosystem.PYTHON, content, content))
    _print_report(report)
    assert report.staging_status == "STAGED"
    assert report.status.value == "FAILED"


def test_npm_offline_staging_install_and_tests(workspace_root):
    _require_docker()
    repo = workspace_root / "repos" / "npm-fixture"
    repo.mkdir(parents=True)
    test_script = "node -e \"const lp=require('left-pad'); if (lp('1', 3, '0') !== '001') process.exit(1); console.log('ok')\""
    original = json.dumps({"name": "fixture", "version": "1.0.0", "dependencies": {"left-pad": "1.2.0"},
                           "scripts": {"test": test_script}}, indent=2)
    proposed = original.replace('"1.2.0"', '"1.3.0"')
    (repo / "package.json").write_text(original)
    report = _validate(repo, workspace_root, _proposal("package.json", Ecosystem.NPM, original, proposed))
    _print_report(report)

    assert report.staging_status == "STAGED"
    names = {c.command_name: c for c in report.commands}
    assert names["dependency_install"].is_passed, "offline npm ci inside the container failed"
    assert names["test_execution"].is_passed
    assert report.status.value == "PASSED"
