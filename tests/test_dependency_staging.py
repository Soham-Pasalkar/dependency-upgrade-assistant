"""Offline dependency staging (FR-18, ADR-007, T-20). Host tools are replaced by fakes: no network."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from src.models.manifest import Ecosystem
from src.models.plan import PatchIntegrityResult, ProposalStatus, UpgradeProposalResponse
from src.models.sandbox import SandboxExecutionRequest
from src.sandbox import dependency_staging as ds
from src.sandbox.dependency_staging import DependencyStagingManager as D
from src.sandbox.runner import DockerAvailability, SandboxExecutionResult
from src.services.proposal_registry import ProposalRegistry
from src.services.sandbox_service import SandboxService, with_staged_site_packages


def test_without_manifest_nothing_is_staged():
    r = D.prepare_staging_cache("python")
    assert r.status == "NOT_REQUIRED"
    r.cleanup()


@pytest.mark.parametrize("content", ["-e .\n", "git+https://github.com/a/b\n", "./local-pkg\n", "https://x.example/p.whl\n"])
def test_python_local_or_url_requirements_are_unstageable(content):
    r = D.prepare_staging_cache("python", manifest_content=content, runner=lambda *a, **k: pytest.fail("must not run"))
    assert r.status == "UNSTAGEABLE"


def test_python_nested_requirement_files_are_unstageable():
    r = D.prepare_staging_cache("python", manifest_content="-r base.txt\nrequests\n", runner=lambda *a, **k: pytest.fail())
    assert r.status == "UNSTAGEABLE"


def test_pip_command_is_wheels_only_and_isolated(tmp_path):
    cmd = ds.build_pip_download_command(tmp_path / "r.txt", tmp_path / "out", arch="x86_64")
    assert "--only-binary=:all:" in cmd and "--isolated" in cmd and "--no-cache-dir" in cmd
    assert cmd[cmd.index("--index-url") + 1] == "https://pypi.org/simple"
    assert "manylinux2014_x86_64" in cmd and "any" in cmd
    assert cmd[cmd.index("--python-version") + 1] == "3.11"


def test_host_tool_env_has_no_secrets_and_pins_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
    monkeypatch.setenv("NPM_TOKEN", "leak")
    env = ds.host_tool_env(tmp_path)
    assert "AWS_SECRET_ACCESS_KEY" not in env and "NPM_TOKEN" not in env
    assert env["NPM_CONFIG_IGNORE_SCRIPTS"] == "true"
    assert env["NPM_CONFIG_REGISTRY"] == "https://registry.npmjs.org/"
    assert env["PIP_CONFIG_FILE"] == os.devnull


def _fake_pip(files):
    def run(cmd, cwd, watch, env, **_):
        dest = Path(cmd[cmd.index("--dest") + 1])
        for name, data in files.items():
            (dest / name).write_bytes(data)
        return 0, ""
    return run


def test_python_staging_records_hashes():
    r = D.prepare_staging_cache("python", manifest_content="requests==2.32.3\n",
                                runner=_fake_pip({"requests-2.32.3-py3-none-any.whl": b"wheel"}))
    assert r.status == "STAGED" and r.artifact_count == 1
    assert r.staged_artifacts[0].startswith("requests-2.32.3-py3-none-any.whl sha256:")
    r.cleanup()


def test_python_staging_rejects_non_wheel_artifacts():
    r = D.prepare_staging_cache("python", manifest_content="x==1\n", runner=_fake_pip({"x-1.tar.gz": b"sdist"}))
    assert r.status == "FAILED"


def test_python_no_wheel_is_reported_unstageable():
    r = D.prepare_staging_cache("python", manifest_content="pycrypto==2.6.1\n",
                                runner=lambda *a, **k: (1, "ERROR: No matching distribution found for pycrypto==2.6.1"))
    assert r.status == "UNSTAGEABLE" and "source builds" in r.message


def _fake_npm(lock_packages, ci_code=0):
    def run(cmd, cwd, watch, env, **_):
        if "--package-lock-only" in cmd:
            (Path(cwd) / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": lock_packages}))
            return 0, ""
        cache = Path(cmd[cmd.index("--cache") + 1])
        (cache / "_cacache").mkdir(parents=True, exist_ok=True)
        (cache / "_cacache" / "blob").write_bytes(b"x" * 10)
        return ci_code, "" if ci_code == 0 else "npm ERR! integrity mismatch"
    return run


PKG = json.dumps({"dependencies": {"left-pad": "^1.3.0"}})


def test_npm_staging_produces_lockfile_and_cache():
    r = D.prepare_staging_cache("npm", manifest_content=PKG, manifest_rel_path="package.json", runner=_fake_npm({
        "": {}, "node_modules/left-pad": {"resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz",
                                          "integrity": "sha512-abc"},
    }))
    assert r.status == "STAGED" and r.artifact_count == 1
    assert r.lockfile_path.exists() and (r.npm_cache_dir / "_cacache").exists()
    r.cleanup()


def test_npm_foreign_tarball_hosts_are_blocked():
    r = D.prepare_staging_cache("npm", manifest_content=PKG, runner=_fake_npm({
        "node_modules/left-pad": {"resolved": "https://evil.example/left-pad.tgz"},
    }))
    assert r.status == "REGISTRY_BLOCKED"


def test_npm_integrity_failure_is_reported():
    r = D.prepare_staging_cache("npm", manifest_content=PKG, runner=_fake_npm({
        "node_modules/left-pad": {"resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.3.0.tgz"},
    }, ci_code=1))
    assert r.status == "FAILED" and any("integrity" in d for d in r.diagnostics)


@pytest.mark.parametrize("spec", ["github:a/b", "file:../x", "git+https://x/y.git", "https://x/y.tgz", "workspace:*"])
def test_npm_non_registry_specs_are_unstageable(spec):
    r = D.prepare_staging_cache("npm", manifest_content=json.dumps({"dependencies": {"x": spec}}),
                                runner=lambda *a, **k: pytest.fail("must not run"))
    assert r.status == "UNSTAGEABLE"


def test_npm_existing_lockfile_is_used(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package-lock.json").write_text('{"lockfileVersion": 3, "packages": {}}')
    seen = {}

    def run(cmd, cwd, watch, env, **kw):
        if "--package-lock-only" in cmd:
            seen["had_lock"] = (Path(cwd) / "package-lock.json").exists()
        return _fake_npm({"": {}})(cmd, cwd, watch, env, **kw)

    r = D.prepare_staging_cache("npm", manifest_content=PKG, manifest_rel_path="web/package.json",
                                source_workspace=tmp_path, runner=run)
    assert seen["had_lock"] is True and "repository's package-lock.json" in r.message
    r.cleanup()


@pytest.mark.live_staging
def test_staging_watchdog_enforces_byte_cap(tmp_path):
    code = "import os,time\nfor i in range(400):\n    open(f'f{i}','wb').write(b'x'*20000); time.sleep(0.01)\n"
    with pytest.raises(RuntimeError, match="exceeded"):
        ds.run_bounded([sys.executable, "-c", code], tmp_path, tmp_path, {"PATH": os.environ["PATH"]}, max_bytes=200_000)


def test_pytest_command_gets_staged_site_packages():
    cmd = with_staged_site_packages(["pytest", "-q", "tests"])
    assert cmd[:2] == ["python", "-c"] and "/workspace/working/.dua_site" in cmd[2] and "'-q', 'tests'" in cmd[2]
    assert with_staged_site_packages(["npm", "test"]) == ["npm", "test"]


def _proposal(manifest_type, path, content):
    return UpgradeProposalResponse(
        manifest_type=manifest_type, ecosystem=Ecosystem.NPM if manifest_type == "package.json" else Ecosystem.PYTHON,
        file_path=path, original_content=content, proposed_content=content, unified_diff="",
        integrity=PatchIntegrityResult(is_valid=True, can_apply=True, can_reverse=True, syntax_valid=True,
                                       target_versions_match=True, unrelated_dependencies_unchanged=True,
                                       original_unchanged=True),
        status=ProposalStatus.READY_FOR_REVIEW,
    )


def _run_with_fakes(proposal, staging_result):
    registry = ProposalRegistry()
    pid = registry.store(proposal)
    service = SandboxService(proposal_registry=registry)
    calls = []

    def fake_run(command_args, working_dir, **kw):
        calls.append((command_args, Path(working_dir), kw.get("staging_dir")))
        return SandboxExecutionResult(exit_code=0, stdout="ok", stderr="", duration_seconds=0.1)

    with patch.object(service.runner, "check_availability", return_value=DockerAvailability(is_available=True, version="x")), \
         patch.object(service, "get_availability", return_value=DockerAvailability(is_available=True, version="x")), \
         patch.object(service.runner, "run_command", side_effect=fake_run), \
         patch.object(ds.DependencyStagingManager, "prepare_staging_cache", return_value=staging_result):
        report = service.execute_validation(SandboxExecutionRequest(proposal_id=pid))
    return report, calls


def test_staged_python_validation_installs_before_checks(tmp_path):
    cache = tmp_path / "cache"
    (cache / "python").mkdir(parents=True)
    staged = ds.StagingResult(success=True, status="STAGED", cache_dir=cache, ecosystem="python", message="Staged 1 wheels")
    report, calls = _run_with_fakes(_proposal("requirements.txt", "requirements.txt", "requests==2.32.3\n"), staged)
    names = [c.command_name for c in report.commands]
    assert names[0] == "dependency_install"
    install = calls[0][0]
    assert install[:3] == ["python", "-m", "pip"] and "--no-index" in install and "/cache/python" in install
    assert calls[0][2] == cache
    assert any("Dependency staging: Staged 1 wheels" in d for d in report.diagnostics)


def test_unstaged_validation_reports_why(tmp_path):
    unstaged = ds.StagingResult(success=False, status="UNSTAGEABLE", cache_dir=tmp_path,
                                message="Some packages have no prebuilt wheel")
    report, calls = _run_with_fakes(_proposal("requirements.txt", "requirements.txt", "x==1\n"), unstaged)
    assert [c.command_name for c in report.commands][0] == "py_compile"
    assert report.staging_status == "UNSTAGEABLE"
    assert any("no prebuilt wheel" in d for d in report.diagnostics)


def test_staged_npm_validation_copies_cache_and_lockfile(tmp_path):
    cache = tmp_path / "cache"
    (cache / "npm-cache" / "_cacache").mkdir(parents=True)
    lock = cache / "package-lock.json"
    lock.write_text('{"lockfileVersion": 3}')
    staged = ds.StagingResult(success=True, status="STAGED", cache_dir=cache, ecosystem="npm",
                              lockfile_path=lock, npm_cache_dir=cache / "npm-cache", message="Staged 3 npm packages")
    report, calls = _run_with_fakes(_proposal("package.json", "package.json", '{"dependencies": {"a": "1.0.0"}}'), staged)
    install, workdir, _ = calls[0]
    assert install[:2] == ["npm", "ci"] and "--offline" in install and "--ignore-scripts" in install
    assert "/workspace/working/.dua_npm_cache" in install
    assert report.commands[0].command_name == "dependency_install"


# --- Untrusted manifest content must not steer host-side fetches (audit F2/F3) ---------------

@pytest.mark.parametrize("content", [
    "--extra-index-url http://169.254.169.254/simple\nsix==1.16.0\n",
    "--index-url http://evil.example/simple\nsix\n",
    "-i http://evil.example/simple\nsix\n",
    "--find-links http://evil.example/\nsix\n",
    "--trusted-host evil.example\nsix\n",
    "six @ http://127.0.0.1:9/six-1.16.0-py3-none-any.whl\n",
    "six==1.16.0 --config-settings x=y\n",
    "six==1.16.0 --global-option=--x\n",
    "not a valid requirement !!!\n",
])
def test_python_options_and_urls_are_refused_before_pip_runs(content):
    r = D.prepare_staging_cache("python", manifest_content=content, runner=lambda *a, **k: pytest.fail("pip must not run"))
    assert r.status == "UNSTAGEABLE"


def test_pip_only_reads_the_sanitized_file():
    seen = {}

    def run(cmd, cwd, watch, env, **_):
        seen["text"] = Path(cmd[cmd.index("-r") + 1]).read_text()
        return 0, ""

    content = "# comment\nrequests[socks]>=2.0; python_version > '3.8'  # trailing\nsix==1.16.0 \\\n  --hash=sha256:" + "a" * 64 + "\n"
    D.prepare_staging_cache("python", manifest_content=content, runner=run)
    assert "#" not in seen["text"]
    assert 'requests[socks]>=2.0; python_version > "3.8"' in seen["text"]
    assert "six==1.16.0 --hash=sha256:" + "a" * 64 in seen["text"]


@pytest.mark.parametrize("pkg", [
    {"dependencies": {"x": "1.0.0"}, "overrides": {"y": "https://evil.example/y.tgz"}},
    {"dependencies": {"x": "1.0.0"}, "overrides": {"z": {"y": "github:evil/y"}}},
    {"dependencies": {"x": "1.0.0"}, "resolutions": {"y": "file:../y"}},
    {"dependencies": {"x": "evil/repo"}},
    {"dependencies": {"x": "1.0.0"}, "workspaces": ["packages/*"]},
])
def test_npm_non_registry_overrides_and_shorthands_are_refused(pkg):
    r = D.prepare_staging_cache("npm", manifest_content=json.dumps(pkg), runner=lambda *a, **k: pytest.fail("npm must not run"))
    assert r.status == "UNSTAGEABLE"


def test_npm_alias_and_ranges_are_allowed():
    pkg = {"dependencies": {"a": "^1.2.0", "b": "npm:@scope/b@2.0.0", "c": "latest", "d": ">=1 <2 || 3.x"}}
    r = D.prepare_staging_cache("npm", manifest_content=json.dumps(pkg), runner=_fake_npm({"": {}}))
    assert r.status == "STAGED"
    r.cleanup()


def test_npm_repository_lockfile_with_foreign_hosts_is_blocked_before_npm_runs(tmp_path):
    (tmp_path / "package-lock.json").write_text(json.dumps({"lockfileVersion": 3, "packages": {
        "node_modules/x": {"resolved": "http://169.254.169.254/x.tgz"}}}))
    r = D.prepare_staging_cache("npm", manifest_content=PKG, manifest_rel_path="package.json",
                                source_workspace=tmp_path, runner=lambda *a, **k: pytest.fail("npm must not run"))
    assert r.status == "REGISTRY_BLOCKED"
