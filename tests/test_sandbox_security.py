"""Security and containment verification tests for M5 Sandboxed Validation subsystem."""
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
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
    get_platform_execution_user,
    is_root_user,
    sanitize_resource_limits,
    validate_and_resolve_user,
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
    validate_snapshot_permissions,
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


def test_workspace_boundary_rejects_root_directory_and_dot_references(tmp_path: Path):
    """CRITICAL SECURITY TEST: Ensure workspace path cannot equal workspace root itself or use dot references."""
    ws_root = tmp_path / "valid_root"
    ws_root.mkdir()

    for dot_path in [".", "./", ".//", "my_project/.."]:
        with pytest.raises(InvalidWorkspaceError):
            validate_trusted_workspace_path(dot_path, workspace_root=ws_root)

    # Directly verify is_safe_relative_path rejects dot segments
    assert is_safe_relative_path(".") is False
    assert is_safe_relative_path("./") is False
    assert is_safe_relative_path("foo/./bar") is False
    assert is_safe_relative_path("..") is False
    assert is_safe_relative_path("foo/../bar") is False


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


# ----------------------------------------------------------------------
# Section 10: Snapshot Filesystem Permissions & User Isolation Tests
# ----------------------------------------------------------------------

def test_snapshot_directories_forbid_world_writable_and_other_permissions(tmp_path: Path):
    """CRITICAL SECURITY TEST: Ensure snapshot directories are strictly 0700 and never world-writable (0777)."""
    src_dir = tmp_path / "src_for_perm_check"
    src_dir.mkdir()
    sub_dir = src_dir / "pkg" / "nested"
    sub_dir.mkdir(parents=True)
    (src_dir / "setup.py").write_text("# dummy")
    (sub_dir / "module.py").write_text("x = 1")

    snapshot = SnapshotManager.create_snapshot(source_workspace=src_dir)
    try:
        snap_path = snapshot.path
        assert snap_path.exists()

        # 1. Root snapshot directory permissions
        root_stat = snap_path.stat()
        root_mode = root_stat.st_mode
        assert (root_mode & 0o002) == 0, "Snapshot root directory must NOT be world-writable!"
        assert (root_mode & 0o007) == 0, "Snapshot root directory must have zero permissions for 'others'!"
        assert (root_mode & 0o700) == 0o700, "Snapshot root directory must have rwx for owner!"
        assert validate_snapshot_permissions(snap_path) is True

        # 2. All nested subdirectories and files
        for root, dirs, files in os.walk(snap_path):
            for d in dirs:
                dir_path = Path(root) / d
                d_stat = dir_path.stat()
                d_mode = d_stat.st_mode
                assert (d_mode & 0o002) == 0, f"Snapshot subdirectory {dir_path.name} must NOT be world-writable!"
                assert (d_mode & 0o007) == 0, f"Snapshot subdirectory {dir_path.name} must have zero 'others' permissions!"
                assert (d_mode & 0o700) == 0o700, f"Snapshot subdirectory {dir_path.name} must have rwx for owner!"
                assert validate_snapshot_permissions(dir_path) is True

            for f in files:
                file_path = Path(root) / f
                f_stat = file_path.stat()
                f_mode = f_stat.st_mode
                assert (f_mode & 0o002) == 0, f"Snapshot file {file_path.name} must NOT be world-writable!"
                assert (f_mode & 0o020) == 0, f"Snapshot file {file_path.name} must NOT be group-writable!"
                assert (f_mode & 0o400) == 0o400, f"Snapshot file {file_path.name} must be readable by owner!"

        # 3. Test validate_snapshot_permissions rejects world-writable directories
        insecure_test_dir = tmp_path / "insecure_0777"
        insecure_test_dir.mkdir()
        os.chmod(insecure_test_dir, 0o777)
        assert validate_snapshot_permissions(insecure_test_dir) is False

        # 4. Test validate_snapshot_permissions rejects world-readable directories
        readable_test_dir = tmp_path / "readable_0755"
        readable_test_dir.mkdir()
        os.chmod(readable_test_dir, 0o755)
        assert validate_snapshot_permissions(readable_test_dir) is False
    finally:
        snapshot.cleanup()


def test_container_user_resolution_matches_host_non_root_identity():
    """CRITICAL SECURITY TEST: Ensure default container user matches host identity and is strictly non-root."""
    resolved_user = get_platform_execution_user()
    assert resolved_user is not None
    assert len(resolved_user) > 0

    parts = resolved_user.split(":")
    uid = int(parts[0])
    assert uid != 0, "Container user MUST NEVER be root (UID 0)!"

    # Verify SandboxSecurityConfig sets resolved user by default
    config = SandboxSecurityConfig()
    assert config.user == resolved_user
    assert not is_root_user(config.user)


def test_container_config_rejects_root_user_execution(tmp_path: Path):
    """CRITICAL SECURITY TEST: Any attempt to execute as root inside the container must be rejected fail-closed."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    for forbidden_user in ["0", "0:0", "root", "ROOT", "0:1000", "root:0"]:
        # 1. Direct configuration rejection
        with pytest.raises(ValueError, match="Root container execution .* is strictly forbidden"):
            SandboxSecurityConfig(user=forbidden_user)

        # 2. Builder validation rejection
        with pytest.raises(ValueError, match="Root container execution .* is strictly forbidden"):
            validate_and_resolve_user(forbidden_user)


def test_snapshot_creation_and_cleanup_lifecycle_with_readonly_artifacts(tmp_path: Path):
    """Verify snapshot creation, modification, and clean removal even with read-only test artifacts."""
    src_dir = tmp_path / "src_repo_lifecycle"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("print('hello')")

    snapshot = SnapshotManager.create_snapshot(source_workspace=src_dir)
    snap_path = snapshot.path
    assert snap_path.exists()

    # Simulate locked test artifacts created by a test run
    locked_dir = snap_path / ".cache"
    locked_dir.mkdir(mode=0o700)
    locked_file = locked_dir / "readonly.dat"
    locked_file.write_text("immutable")
    os.chmod(locked_file, 0o400)  # read-only file

    # Execute cleanup
    snapshot.cleanup()
    assert not snap_path.exists(), "Snapshot cleanup must remove directory even when read-only artifacts exist!"


def test_snapshot_creation_failure_fails_closed(tmp_path: Path):
    """Verify snapshot creation fails closed without leaking directories when source is invalid."""
    bad_source = tmp_path / "does_not_exist_xyz"
    with pytest.raises(SnapshotCreationError, match="Source workspace does not exist"):
        SnapshotManager.create_snapshot(source_workspace=bad_source)


def test_snapshot_creation_normal_non_root_host_identity(tmp_path: Path):
    """Verify normal non-root host execution sets 0700 permissions and matching UID ownership."""
    src_dir = tmp_path / "non_root_src"
    src_dir.mkdir()
    (src_dir / "app.py").write_text("print('test')")
    sub = src_dir / "nested"
    sub.mkdir()
    (sub / "util.py").write_text("def f(): pass")

    snapshot = SnapshotManager.create_snapshot(source_workspace=src_dir)
    try:
        snap_path = snapshot.path
        assert snap_path.exists()
        assert validate_snapshot_permissions(snap_path) is True

        # Owner must match current host user
        if hasattr(os, "getuid"):
            assert snap_path.stat().st_uid == os.getuid()
            assert (snap_path / "nested").stat().st_uid == os.getuid()
            assert (snap_path / "app.py").stat().st_uid == os.getuid()

        # Permissions must be strictly 0700 for directories
        assert (snap_path.stat().st_mode & 0o007) == 0
        assert ((snap_path / "nested").stat().st_mode & 0o007) == 0

        # Files must not have group or world write
        assert ((snap_path / "app.py").stat().st_mode & 0o022) == 0
    finally:
        snapshot.cleanup()


def test_snapshot_creation_simulated_root_host_fallback(tmp_path: Path):
    """SIMULATED ROOT-HOST TEST: Verify root host assigns non-root container ownership to all snapshot artifacts."""
    src_dir = tmp_path / "root_src"
    src_dir.mkdir()
    (src_dir / "index.py").write_text("x = 1")
    sub = src_dir / "subdir"
    sub.mkdir()
    (sub / "subfile.py").write_text("y = 2")

    chown_calls = []

    def mock_chown(path, uid, gid):
        chown_calls.append((str(path), uid, gid))

    # Mock os.getuid to return 0 (root) and mock os.chown
    with patch("os.getuid", return_value=0), patch("os.chown", side_effect=mock_chown):
        # Also mock stat st_uid to return 1000 so post-creation ownership verification passes
        orig_stat = Path.stat

        def mock_stat_fn(self_path):
            st = orig_stat(self_path)
            # Create a mock stat_result with st_uid=1000
            class StatWrapper:
                def __init__(self, orig):
                    self._orig = orig
                    self.st_mode = orig.st_mode
                    self.st_uid = 1000
                    self.st_gid = 1000
                def __getattr__(self, name):
                    return getattr(self._orig, name)
            return StatWrapper(st)

        with patch.object(Path, "stat", mock_stat_fn):
            snapshot = SnapshotManager.create_snapshot(
                source_workspace=src_dir,
                container_user="1000:1000",
            )
            try:
                snap_path = snapshot.path
                assert snap_path.exists()

                # Verify chown was invoked for:
                # 1. Snapshot root directory
                # 2. Subdirectory
                # 3. Root file
                # 4. Nested file
                assert len(chown_calls) >= 4
                for path_str, uid, gid in chown_calls:
                    assert uid == 1000
                    assert gid == 1000
            finally:
                snapshot.cleanup()


def test_snapshot_creation_fails_closed_on_chmod_failure(tmp_path: Path):
    """CRITICAL SECURITY TEST: Ensure permission failure on snapshot root raises SnapshotCreationError and cleans up."""
    src_dir = tmp_path / "src_chmod_fail"
    src_dir.mkdir()

    created_dirs = []
    orig_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(**kwargs):
        res = orig_mkdtemp(**kwargs)
        created_dirs.append(res)
        return res

    with patch("tempfile.mkdtemp", side_effect=tracking_mkdtemp):
        with patch("os.chmod", side_effect=OSError("Disk read-only or EPERM")):
            with pytest.raises(SnapshotCreationError, match="Failed applying secure directory permissions"):
                SnapshotManager.create_snapshot(source_workspace=src_dir)

    # Verify that the created temporary directory was cleaned up and does not linger on disk
    assert len(created_dirs) == 1
    assert not Path(created_dirs[0]).exists(), "Failed snapshot creation must not leak orphaned directories!"


def test_snapshot_creation_fails_closed_on_chown_failure_when_root(tmp_path: Path):
    """SIMULATED ROOT-HOST TEST: Ensure chown failure under root raises SnapshotCreationError and cleans up."""
    src_dir = tmp_path / "src_chown_fail"
    src_dir.mkdir()

    created_dirs = []
    orig_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(**kwargs):
        res = orig_mkdtemp(**kwargs)
        created_dirs.append(res)
        return res

    with patch("tempfile.mkdtemp", side_effect=tracking_mkdtemp):
        with patch("os.getuid", return_value=0):
            with patch("os.chown", side_effect=OSError("Operation not permitted")):
                with pytest.raises(SnapshotCreationError, match="Root host fallback failed assigning container ownership"):
                    SnapshotManager.create_snapshot(source_workspace=src_dir, container_user="1000:1000")

    assert len(created_dirs) == 1
    assert not Path(created_dirs[0]).exists(), "Failed root fallback snapshot must clean up immediately!"


def test_snapshot_proposal_overlay_ownership_and_permissions(tmp_path: Path):
    """Verify proposal overlay file has 0600 permissions and no group/world write access."""
    src_dir = tmp_path / "src_overlay"
    src_dir.mkdir()
    (src_dir / "requirements.txt").write_text("urllib3==1.26.5\n")

    proposal = UpgradeProposalResponse(
        proposal_id="prop-123",
        manifest_type="requirements.txt",
        ecosystem=Ecosystem.PYTHON,
        file_path="requirements.txt",
        plan_items=[],
        total_planned=1,
        total_unchanged=0,
        total_conflicts=0,
        original_content="urllib3==1.26.5\n",
        proposed_content="urllib3==2.2.1\n",
        unified_diff="--- a/requirements.txt\n+++ b/requirements.txt\n",
        integrity=PatchIntegrityResult(
            is_valid=True,
            can_apply=True,
            can_reverse=True,
            syntax_valid=True,
            target_versions_match=True,
            unrelated_dependencies_unchanged=True,
            original_unchanged=True,
            checks_passed=["syntax"],
        ),
        status=ProposalStatus.READY_FOR_REVIEW,
    )

    snapshot = SnapshotManager.create_snapshot(source_workspace=src_dir, proposal=proposal)
    try:
        manifest_file = snapshot.manifest_path
        assert manifest_file is not None
        assert manifest_file.exists()
        assert manifest_file.read_text(encoding="utf-8") == "urllib3==2.2.1\n"

        # Check permissions: owner read/write, zero group/world write
        st = manifest_file.stat()
        assert (st.st_mode & 0o022) == 0, "Overlay file must not have group or world write bits!"
        assert (st.st_mode & 0o600) == 0o600, "Overlay file must have owner read/write!"
        assert snapshot.manifest.has_proposal_overlay is True
        assert snapshot.manifest.overlay_target == "requirements.txt"
    finally:
        snapshot.cleanup()


def test_snapshot_ownership_mismatch_raises_and_cleans_up(tmp_path: Path):
    """Verify that an ownership mismatch detected in verification raises SnapshotCreationError and cleans up."""
    src_dir = tmp_path / "src_mismatch"
    src_dir.mkdir()

    created_dirs = []
    orig_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(**kwargs):
        res = orig_mkdtemp(**kwargs)
        created_dirs.append(res)
        return res

    orig_stat = Path.stat
    def mock_mismatched_stat(self_path):
        st = orig_stat(self_path)
        class MismatchStat:
            def __init__(self, orig):
                self._orig = orig
                self.st_mode = orig.st_mode
                self.st_uid = 99999  # Mismatched UID
                self.st_gid = 99999
            def __getattr__(self, name):
                return getattr(self._orig, name)
        return MismatchStat(st)

    with patch("tempfile.mkdtemp", side_effect=tracking_mkdtemp):
        with patch.object(Path, "stat", mock_mismatched_stat):
            with pytest.raises(SnapshotCreationError, match="Snapshot directory ownership mismatch"):
                SnapshotManager.create_snapshot(source_workspace=src_dir)

    assert len(created_dirs) == 1
    assert not Path(created_dirs[0]).exists(), "Mismatched ownership snapshot must be cleaned up!"
