"""Security and containment verification tests for M5 Sandboxed Validation subsystem."""
import os
from pathlib import Path
import pytest

from src.models.manifest import Ecosystem
from src.models.plan import PatchIntegrityResult, ProposalStatus, UpgradeProposalResponse
from src.sandbox.container_config import ContainerConfigBuilder, SandboxSecurityConfig
from src.sandbox.image_catalog import (
    ALL_ALLOWLISTED_IMAGES,
    DEFAULT_NODE_IMAGE,
    DEFAULT_PYTHON_IMAGE,
    get_default_image_for_ecosystem,
    is_image_allowlisted,
    validate_image_or_raise,
)
from src.sandbox.snapshot_manager import SnapshotManager, is_sensitive_entry


def test_container_config_enforces_all_mandatory_security_flags(tmp_path: Path):
    """CRITICAL SECURITY TEST: Ensure ContainerConfigBuilder includes all isolation constraints."""
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()

    config = SandboxSecurityConfig(
        image="python:3.11-slim",
        network_enabled=False,
        memory_limit="512m",
        cpu_limit="1.0",
        pids_limit=64,
        user="1000:1000",
        read_only_root=True,
    )

    args = ContainerConfigBuilder.build_run_args(
        config=config,
        working_dir=work_dir,
        reference_dir=ref_dir,
        command_args=["pytest", "-q"],
    )

    cmd_str = " ".join(args)

    # 1. Automatic container removal
    assert "--rm" in args

    # 2. Non-root user execution
    assert "--user=1000:1000" in args

    # 3. Network disabled by default
    assert "--network=none" in args

    # 4. Resource quotas
    assert "--memory=512m" in args
    assert "--cpus=1.0" in args
    assert "--pids-limit=64" in args

    # 5. Dropped capabilities & no privilege escalation
    assert "--cap-drop=ALL" in args
    assert "--security-opt=no-new-privileges:true" in args

    # 6. Read-only root filesystem with isolated tmpfs
    assert "--read-only" in args
    assert any("--tmpfs=/tmp" in a for a in args)
    assert any("--tmpfs=/workspace/scratch" in a for a in args)

    # 7. Strictly prohibited flags
    assert "--privileged" not in args
    assert "docker.sock" not in cmd_str
    assert "--network=host" not in args

    # 8. Volume mounts
    assert f"-v {work_dir.resolve()}:/workspace/working:rw" in cmd_str
    assert f"-v {ref_dir.resolve()}:/workspace/reference:ro" in cmd_str


def test_container_config_rejects_docker_socket_mount(tmp_path: Path):
    """CRITICAL SECURITY TEST: Verify any attempt to mount docker.sock is strictly rejected."""
    fake_sock_dir = tmp_path / "var_run_docker.sock"
    fake_sock_dir.mkdir()

    config = SandboxSecurityConfig()

    with pytest.raises(ValueError, match="Forbidden mount: Docker socket mounting is strictly prohibited"):
        ContainerConfigBuilder.build_run_args(
            config=config,
            working_dir=fake_sock_dir,
        )


def test_image_catalog_enforces_strict_allowlist():
    """CRITICAL SECURITY TEST: Only allowlisted base images may be executed."""
    assert is_image_allowlisted("python:3.11-slim") is True
    assert is_image_allowlisted("python:3.12-slim") is True
    assert is_image_allowlisted("node:20-slim") is True
    assert is_image_allowlisted("node:22-slim") is True

    # Validate image returns cleaned string
    assert validate_image_or_raise("PYTHON:3.11-SLIM") == "python:3.11-slim"

    # Strictly reject arbitrary/unapproved images
    for bad_image in [
        "ubuntu:latest",
        "alpine:latest",
        "attacker/malicious:latest",
        "python:3.11",
        "python:3.10-slim",
        "node:latest",
        "debian:bookworm",
        "",
        "   ",
    ]:
        with pytest.raises(ValueError):
            validate_image_or_raise(bad_image)


def test_image_catalog_resolves_default_by_ecosystem():
    """Verify ecosystem mapping defaults to proper allowlisted images."""
    assert get_default_image_for_ecosystem("python") == DEFAULT_PYTHON_IMAGE
    assert get_default_image_for_ecosystem("requirements.txt") == DEFAULT_PYTHON_IMAGE
    assert get_default_image_for_ecosystem("npm") == DEFAULT_NODE_IMAGE
    assert get_default_image_for_ecosystem("package.json") == DEFAULT_NODE_IMAGE


def test_snapshot_scrubs_sensitive_files(tmp_path: Path):
    """CRITICAL SECURITY TEST: Ensure .env, .git, and private keys are never copied into sandbox."""
    src_dir = tmp_path / "source_repo"
    src_dir.mkdir()

    # Normal files
    (src_dir / "app.py").write_text("print('hello')")
    (src_dir / "requirements.txt").write_text("requests==2.32.0\n")

    # Sensitive files to exclude
    (src_dir / ".env").write_text("SAMPLE_VAR=mockvalue")
    (src_dir / ".env.production").write_text("ANOTHER_VAR=mockvalue")
    (src_dir / "id_rsa").write_text("mock-rsa-key-content")
    (src_dir / "cert.pem").write_text("mock-certificate-content")
    git_dir = src_dir / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[core]\nrepositoryformatversion = 0\n")

    snapshot = SnapshotManager.create_snapshot(source_workspace=src_dir)
    try:
        snap_path = snapshot.path
        # Normal files exist
        assert (snap_path / "app.py").exists()
        assert (snap_path / "requirements.txt").exists()

        # Sensitive files were scrubbed
        assert not (snap_path / ".env").exists()
        assert not (snap_path / ".env.production").exists()
        assert not (snap_path / "id_rsa").exists()
        assert not (snap_path / "cert.pem").exists()
        assert not (snap_path / ".git").exists()
    finally:
        snapshot.cleanup()


def test_snapshot_omits_symlinks_escaping_workspace(tmp_path: Path):
    """CRITICAL SECURITY TEST: Ensure symlinks pointing outside workspace root are not followed or copied."""
    outside_dir = tmp_path / "outside_host_dir"
    outside_dir.mkdir()
    secret_file = outside_dir / "secret.txt"
    secret_file.write_text("SENSITIVE_HOST_DATA")

    src_dir = tmp_path / "source_repo"
    src_dir.mkdir()
    (src_dir / "main.py").write_text("print(1)")

    # Create symlink pointing outside
    malicious_symlink = src_dir / "symlink_escape"
    try:
        os.symlink(str(secret_file), str(malicious_symlink))
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not supported in current environment.")

    snapshot = SnapshotManager.create_snapshot(source_workspace=src_dir)
    try:
        snap_path = snapshot.path
        assert (snap_path / "main.py").exists()
        # Escaping symlink must not be copied
        assert not (snap_path / "symlink_escape").exists()
    finally:
        snapshot.cleanup()


def test_snapshot_overlays_proposal_manifest(tmp_path: Path):
    """Verify proposal proposed_content is written to the snapshot."""
    proposal = UpgradeProposalResponse(
        proposal_id="prop-test-456",
        manifest_type="requirements.txt",
        ecosystem=Ecosystem.PYTHON,
        file_path="requirements.txt",
        plan_items=[],
        total_planned=1,
        total_unchanged=0,
        total_conflicts=0,
        original_content="requests==2.28.0\n",
        proposed_content="requests==2.32.0\n",
        unified_diff="--- a/requirements.txt\n+++ b/requirements.txt\n@@ -1 +1 @@\n-requests==2.28.0\n+requests==2.32.0\n",
        integrity=PatchIntegrityResult(
            is_valid=True,
            can_apply=True,
            can_reverse=True,
            syntax_valid=True,
            target_versions_match=True,
            unrelated_dependencies_unchanged=True,
            original_unchanged=True,
            checks_passed=["all"],
            checks_failed=[],
            errors=[],
            warnings=[],
        ),
        status=ProposalStatus.READY_FOR_REVIEW,
        requires_human_approval=True,
        diagnostics=[],
        summary="Bump requests to 2.32.0",
    )

    snapshot = SnapshotManager.create_snapshot(proposal=proposal)
    try:
        manifest_file = snapshot.path / "requirements.txt"
        assert manifest_file.exists()
        assert manifest_file.read_text(encoding="utf-8") == "requests==2.32.0\n"
    finally:
        snapshot.cleanup()
        assert not snapshot.path.exists()
