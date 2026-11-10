"""Security and containment verification tests for M5 Sandboxed Validation subsystem."""
import os
from pathlib import Path
import pytest

from src.models.manifest import Ecosystem
from src.models.plan import PatchIntegrityResult, ProposalStatus, UpgradeProposalResponse
from src.sandbox.container_config import (
    ContainerConfigBuilder,
    SandboxSecurityConfig,
    SERVER_MAX_CPUS,
    SERVER_MAX_MEMORY_MB,
    SERVER_MAX_PIDS,
    SERVER_MAX_TIMEOUT_SECONDS,
    sanitize_resource_limits,
)
from src.sandbox.image_catalog import (
    ALL_ALLOWLISTED_IMAGES,
    DEFAULT_NODE_IMAGE,
    DEFAULT_PYTHON_IMAGE,
    PINNED_IMAGE_DIGESTS,
    get_default_image_for_ecosystem,
    get_image_digest,
    get_pinned_image_reference,
    is_image_allowlisted,
    validate_image_or_raise,
)
from src.sandbox.snapshot_manager import (
    IsolatedSnapshot,
    SecurityException,
    SnapshotCreationError,
    SnapshotManager,
    is_sensitive_entry,
)
from src.sandbox.test_discovery import (
    TestDiscoveryManager,
    TestDiscoveryStatus,
)
from src.sandbox.workspace_boundary import (
    InvalidWorkspaceError,
    is_safe_relative_path,
    validate_trusted_workspace_path,
)


def test_container_config_enforces_all_mandatory_security_flags(tmp_path: Path):
    """CRITICAL SECURITY TEST: Ensure ContainerConfigBuilder includes all isolation constraints."""
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()

    config = SandboxSecurityConfig(
        image="python:3.11-slim",
        network_enabled=False,
        memory_mb=512,
        cpu_limit=1.0,
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


def test_image_catalog_pinned_sha256_digests():
    """CRITICAL SECURITY TEST: Ensure all approved base images map to verified immutable SHA-256 digests."""
    for img in ALL_ALLOWLISTED_IMAGES:
        digest = get_image_digest(img)
        assert digest is not None
        assert digest.startswith("sha256:")
        assert len(digest) == 71  # "sha256:" + 64 hex chars

        pinned_ref = get_pinned_image_reference(img)
        assert pinned_ref == f"{img}@{digest}"


def test_image_catalog_resolves_default_by_ecosystem():
    """Verify ecosystem mapping defaults to proper allowlisted images."""
    assert get_default_image_for_ecosystem("python") == DEFAULT_PYTHON_IMAGE
    assert get_default_image_for_ecosystem("requirements.txt") == DEFAULT_PYTHON_IMAGE
    assert get_default_image_for_ecosystem("npm") == DEFAULT_NODE_IMAGE
    assert get_default_image_for_ecosystem("package.json") == DEFAULT_NODE_IMAGE


def test_resource_limits_server_ceilings_enforced():
    """CRITICAL SECURITY TEST: Ensure resource limit inputs are clamped to server ceilings."""
    # Memory clamped to SERVER_MAX_MEMORY_MB (1024)
    mem, cpu, pids, timeout = sanitize_resource_limits(
        memory_mb=999999,
        cpu_limit=99.9,
        pids_limit=10000,
        timeout_seconds=9999,
    )
    assert mem == SERVER_MAX_MEMORY_MB
    assert cpu == SERVER_MAX_CPUS
    assert pids == SERVER_MAX_PIDS
    assert timeout == SERVER_MAX_TIMEOUT_SECONDS

    # Minimum safety floor
    mem_low, cpu_low, pids_low, timeout_low = sanitize_resource_limits(
        memory_mb=2,
        cpu_limit=0.01,
        pids_limit=2,
        timeout_seconds=1,
    )
    assert mem_low == 64
    assert cpu_low == 0.1
    assert pids_low == 16
    assert timeout_low == 5


# ----------------------------------------------------------------------
# Section 2: Host Filesystem Boundary Tests
# ----------------------------------------------------------------------

def test_workspace_boundary_rejects_etc_and_root(tmp_path: Path):
    """CRITICAL SECURITY TEST: Reject arbitrary host root paths like /etc or /."""
    ws_root = tmp_path / "valid_root"
    ws_root.mkdir()

    with pytest.raises(InvalidWorkspaceError):
        validate_trusted_workspace_path("/etc", workspace_root=ws_root)

    with pytest.raises(InvalidWorkspaceError):
        validate_trusted_workspace_path("/", workspace_root=ws_root)

    with pytest.raises(InvalidWorkspaceError):
        validate_trusted_workspace_path("/etc/passwd", workspace_root=ws_root)


def test_workspace_boundary_rejects_parent_traversals(tmp_path: Path):
    """CRITICAL SECURITY TEST: Reject ../ directory traversal escapes."""
    ws_root = tmp_path / "valid_root"
    ws_root.mkdir()

    for bad_path in ["../", "../../etc", "foo/../../bar", "..", "../evil"]:
        with pytest.raises(InvalidWorkspaceError):
            validate_trusted_workspace_path(bad_path, workspace_root=ws_root)


def test_workspace_boundary_rejects_encoded_traversals_and_null_bytes(tmp_path: Path):
    """CRITICAL SECURITY TEST: Reject URL-encoded traversals and null byte injection."""
    ws_root = tmp_path / "valid_root"
    ws_root.mkdir()

    for encoded_path in [
        "%2e%2e/etc",
        "foo%2f..%2f..%2fbar",
        "..%2f..",
        "%2e%2e%2f",
        "project\0escape",
        "test%00evil",
    ]:
        with pytest.raises(InvalidWorkspaceError):
            validate_trusted_workspace_path(encoded_path, workspace_root=ws_root)


def test_workspace_boundary_rejects_windows_drive_and_unc_paths(tmp_path: Path):
    """CRITICAL SECURITY TEST: Reject Windows drive letters (C:) and UNC shares (\\\\server)."""
    ws_root = tmp_path / "valid_root"
    ws_root.mkdir()

    for win_path in ["C:\\Windows", "C:boot.ini", "D:\\Data", "\\\\server\\share\\repo"]:
        with pytest.raises(InvalidWorkspaceError):
            validate_trusted_workspace_path(win_path, workspace_root=ws_root)


def test_workspace_boundary_rejects_home_directory_escapes(tmp_path: Path):
    """CRITICAL SECURITY TEST: Reject arbitrary home-directory access (~/...)."""
    ws_root = tmp_path / "valid_root"
    ws_root.mkdir()

    for home_path in ["~", "~/.ssh", "~/secret", "~root/secret"]:
        with pytest.raises(InvalidWorkspaceError):
            validate_trusted_workspace_path(home_path, workspace_root=ws_root)


def test_workspace_boundary_rejects_sibling_prefix_directory_confusion(tmp_path: Path):
    """CRITICAL SECURITY TEST: Reject sibling directory prefix confusion (/root_evil vs /root)."""
    ws_root = tmp_path / "project_root"
    ws_root.mkdir()

    sibling_evil = tmp_path / "project_root_evil"
    sibling_evil.mkdir()

    # Even if sibling_evil starts with "project_root", it must be rejected!
    rel_sibling = f"../{sibling_evil.name}"
    with pytest.raises(InvalidWorkspaceError):
        validate_trusted_workspace_path(rel_sibling, workspace_root=ws_root)


def test_workspace_boundary_rejects_symlink_escape(tmp_path: Path):
    """CRITICAL SECURITY TEST: Reject symlink within workspace pointing outside."""
    ws_root = tmp_path / "project_root"
    ws_root.mkdir()

    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()

    symlink_path = ws_root / "escape_link"
    try:
        os.symlink(str(outside_dir), str(symlink_path))
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported in environment.")

    with pytest.raises(InvalidWorkspaceError):
        validate_trusted_workspace_path("escape_link", workspace_root=ws_root)


def test_workspace_boundary_rejects_non_existent_workspace(tmp_path: Path):
    """Verify non-existent directory raises InvalidWorkspaceError."""
    ws_root = tmp_path / "valid_root"
    ws_root.mkdir()

    with pytest.raises(InvalidWorkspaceError, match="does not exist"):
        validate_trusted_workspace_path("non_existent_subproject_12345", workspace_root=ws_root)


def test_workspace_boundary_accepts_valid_subproject(tmp_path: Path):
    """Verify legitimate child directory within workspace root resolves successfully."""
    ws_root = tmp_path / "valid_root"
    ws_root.mkdir()

    subproject = ws_root / "my_project"
    subproject.mkdir()

    resolved = validate_trusted_workspace_path("my_project", workspace_root=ws_root)
    assert resolved == subproject.resolve()


# ----------------------------------------------------------------------
# Section 6: Snapshot Hardening Tests
# ----------------------------------------------------------------------

def test_snapshot_scrubs_comprehensive_sensitive_files(tmp_path: Path):
    """CRITICAL SECURITY TEST: Ensure .env, .git, .ssh, .aws, keys, and tokens are scrubbed."""
    src_dir = tmp_path / "source_repo"
    src_dir.mkdir()

    # Normal code and permitted dotfiles
    (src_dir / "app.py").write_text("print('hello')")
    (src_dir / "requirements.txt").write_text("requests==2.32.0\n")
    (src_dir / ".pytest.ini").write_text("[pytest]\n")
    (src_dir / ".ruff.toml").write_text("line-length = 88\n")

    # Sensitive files and dirs to scrub
    (src_dir / ".env").write_text("SECRET=1")
    (src_dir / ".env.production").write_text("SECRET=2")
    (src_dir / "id_rsa").write_text("rsa-data")
    (src_dir / "id_ed25519").write_text("ed25519-data")
    (src_dir / "cert.pem").write_text("cert-data")
    (src_dir / "server.key").write_text("key-data")
    (src_dir / ".npmrc").write_text("//registry.npmjs.org/:_authToken=secret")
    (src_dir / ".pypirc").write_text("[pypi]\npassword = secret")
    (src_dir / "my_token.txt").write_text("token-secret")
    (src_dir / "db_password.json").write_text("{}")

    # Sensitive dirs
    for s_dir in [".git", ".ssh", ".aws", ".azure", ".gcp", ".kube", ".docker", ".venv"]:
        sd = src_dir / s_dir
        sd.mkdir()
        (sd / "conf").write_text("secret")

    snapshot = SnapshotManager.create_snapshot(source_workspace=src_dir)
    try:
        snap_path = snapshot.path
        # Normal files and safe dotfiles preserved
        assert (snap_path / "app.py").exists()
        assert (snap_path / "requirements.txt").exists()
        assert (snap_path / ".pytest.ini").exists()
        assert (snap_path / ".ruff.toml").exists()

        # Sensitive files were scrubbed
        assert not (snap_path / ".env").exists()
        assert not (snap_path / ".env.production").exists()
        assert not (snap_path / "id_rsa").exists()
        assert not (snap_path / "id_ed25519").exists()
        assert not (snap_path / "cert.pem").exists()
        assert not (snap_path / "server.key").exists()
        assert not (snap_path / ".npmrc").exists()
        assert not (snap_path / ".pypirc").exists()
        assert not (snap_path / "my_token.txt").exists()
        assert not (snap_path / "db_password.json").exists()

        for s_dir in [".git", ".ssh", ".aws", ".azure", ".gcp", ".kube", ".docker", ".venv"]:
            assert not (snap_path / s_dir).exists()

        # Verify snapshot manifest was generated
        assert snapshot.manifest.included_files_count >= 4
        assert snapshot.manifest.excluded_files_count >= 10
    finally:
        snapshot.cleanup()


def test_snapshot_rejects_special_fifo_and_socket_files(tmp_path: Path):
    """CRITICAL SECURITY TEST: Ensure special files (FIFOs, sockets) raise SecurityException."""
    src_dir = tmp_path / "source_with_fifo"
    src_dir.mkdir()
    (src_dir / "test.py").write_text("print(1)")

    fifo_path = src_dir / "test_fifo"
    try:
        os.mkfifo(str(fifo_path))
    except (AttributeError, OSError):
        pytest.skip("mkfifo not supported in environment.")

    with pytest.raises(SecurityException, match="Prohibited special filesystem object"):
        SnapshotManager.create_snapshot(source_workspace=src_dir)


# ----------------------------------------------------------------------
# Section 9: Explicit Test Discovery Tests
# ----------------------------------------------------------------------

def test_test_discovery_python_with_tests(tmp_path: Path):
    """Verify Python test discovery finds test files."""
    repo_dir = tmp_path / "py_repo"
    repo_dir.mkdir()
    tests_dir = repo_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_main.py").write_text("def test_ok(): pass\n")

    res = TestDiscoveryManager.discover_tests(repo_dir, ecosystem="python")
    assert res.status == TestDiscoveryStatus.DISCOVERED
    assert res.framework == "pytest"
    assert res.has_runnable_tests is True
    assert "test_main.py" in res.test_files[0]


def test_test_discovery_python_without_tests(tmp_path: Path):
    """CRITICAL: Python repo with NO test files must report NO_TESTS_FOUND (never false PASSED)."""
    repo_dir = tmp_path / "py_repo_no_tests"
    repo_dir.mkdir()
    (repo_dir / "app.py").write_text("print('hello')\n")

    res = TestDiscoveryManager.discover_tests(repo_dir, ecosystem="python")
    assert res.status == TestDiscoveryStatus.NO_TESTS_FOUND
    assert res.has_runnable_tests is False
    assert len(res.test_files) == 0


def test_test_discovery_node_with_tests(tmp_path: Path):
    """Verify Node.js discovers valid test script."""
    repo_dir = tmp_path / "node_repo"
    repo_dir.mkdir()
    (repo_dir / "package.json").write_text('{"scripts": {"test": "vitest run"}}\n')

    res = TestDiscoveryManager.discover_tests(repo_dir, ecosystem="npm")
    assert res.status == TestDiscoveryStatus.DISCOVERED
    assert res.framework == "npm_test"
    assert res.has_runnable_tests is True


def test_test_discovery_node_default_npm_placeholder_rejected(tmp_path: Path):
    """CRITICAL: Default npm placeholder ('echo Error: no test specified && exit 1') must report NO_TESTS_FOUND."""
    repo_dir = tmp_path / "node_repo_placeholder"
    repo_dir.mkdir()
    (repo_dir / "package.json").write_text('{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}\n')

    res = TestDiscoveryManager.discover_tests(repo_dir, ecosystem="npm")
    assert res.status == TestDiscoveryStatus.NO_TESTS_FOUND
    assert res.has_runnable_tests is False
