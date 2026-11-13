"""Repository acquisition and discovery tests (FR-1R, FR-2R, NFR-9, NFR-10, T-15..T-18)."""
from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from src.services import repository_service as rs


def _resolver(*addrs):
    return lambda host, port, proto=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in addrs]


PUBLIC = _resolver("140.82.112.3")


@pytest.mark.parametrize("url,owner,repo", [
    ("https://github.com/psf/requests", "psf", "requests"),
    ("https://github.com/psf/requests.git", "psf", "requests"),
    ("https://github.com/psf/requests/", "psf", "requests"),
    ("  https://github.com/a-b/c.d_e  ", "a-b", "c.d_e"),
])
def test_valid_urls_are_canonicalized(url, owner, repo):
    ref = rs.parse_repository_url(url)
    assert (ref.owner, ref.repo) == (owner, repo)
    assert ref.clone_url == f"https://github.com/{owner}/{repo}.git"


@pytest.mark.parametrize("url", [
    "http://github.com/a/b", "git@github.com:a/b.git", "ssh://github.com/a/b", "file:///etc/passwd",
    "https://github.com.evil.com/a/b", "https://evil.com/github.com/a/b", "https://user:pw@github.com/a/b",
    "https://github.com:22/a/b", "https://github.com/a/b/tree/main", "https://github.com/a", "https://github.com/a/..",
    "https://github.com/%2e%2e/b", "https://github.com/a/b?x=1", "https://github.com/a/b#frag", "https://127.0.0.1/a/b",
    "https://github.com/-bad/b", "", "https://github.com/a/b\nfoo",
])
def test_unsafe_or_malformed_urls_are_rejected(url):
    with pytest.raises(rs.RepositoryError):
        rs.parse_repository_url(url)


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.1", "::1", "fd00::1"])
def test_non_public_resolution_is_blocked(addr):
    with pytest.raises(rs.RepositoryError) as exc:
        rs._assert_public_resolution("api.github.com", _resolver(addr))
    assert exc.value.code == "unsafe_destination"


def _resp(status, payload=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = payload or {}
    return r


REF = rs.RepositoryRef("octo", "demo")


def test_metadata_success():
    s = MagicMock(spec=requests.Session)
    s.get.return_value = _resp(200, {"full_name": "octo/demo", "default_branch": "main", "size": 100, "private": False})
    meta = rs.fetch_repository_metadata(REF, session=s, resolver=PUBLIC)
    assert meta.full_name == "octo/demo" and meta.size_kb == 100
    assert s.get.call_args.kwargs["allow_redirects"] is False


@pytest.mark.parametrize("status,payload,headers,code", [
    (404, {}, {}, "not_found"),
    (403, {}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 120)}, "rate_limited"),
    (429, {}, {}, "rate_limited"),
    (200, {"private": True, "size": 1}, {}, "private"),
    (200, {"private": False, "size": rs.MAX_REPO_SIZE_KB + 1}, {}, "too_large"),
    (500, {}, {}, "github_error"),
])
def test_metadata_failures_are_typed(status, payload, headers, code):
    s = MagicMock(spec=requests.Session)
    s.get.return_value = _resp(status, payload, headers)
    with pytest.raises(rs.RepositoryError) as exc:
        rs.fetch_repository_metadata(REF, session=s, resolver=PUBLIC)
    assert exc.value.code == code


def test_cross_host_redirect_is_blocked_and_same_host_followed():
    s = MagicMock(spec=requests.Session)
    s.get.return_value = _resp(301, headers={"Location": "https://evil.example/repos/x"})
    with pytest.raises(rs.RepositoryError) as exc:
        rs.fetch_repository_metadata(REF, session=s, resolver=PUBLIC)
    assert exc.value.code == "redirect_blocked"

    s2 = MagicMock(spec=requests.Session)
    s2.get.side_effect = [
        _resp(301, headers={"Location": "https://api.github.com/repositories/1"}),
        _resp(200, {"full_name": "octo/renamed", "size": 1}),
    ]
    assert rs.fetch_repository_metadata(REF, session=s2, resolver=PUBLIC).full_name == "octo/renamed"


def test_clone_command_is_hardened():
    cmd = rs.build_clone_command(REF, Path("/tmp/x"))
    joined = " ".join(cmd)
    for flag in ("core.symlinks=false", "protocol.allow=never", "protocol.https.allow=always",
                 "core.hooksPath=/dev/null", "filter.lfs.smudge=", "http.followRedirects=false"):
        assert flag in joined
    for opt in ("--depth", "--single-branch", "--no-tags", "--no-recurse-submodules"):
        assert opt in cmd
    assert cmd[-2] == "https://github.com/octo/demo.git" and cmd[-3] == "--"


def test_git_environment_does_not_inherit_server_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "should-not-leak")
    env = rs.build_git_env(tmp_path)
    assert "AWS_SECRET_ACCESS_KEY" not in env and "GITHUB_TOKEN" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_ALLOW_PROTOCOL"] == "https"


def _writer(dest: Path, files: int, size: int, delay: float = 0.02):
    code = (
        "import os,sys,time\n"
        f"d=sys.argv[1]; os.makedirs(d, exist_ok=True)\n"
        f"for i in range({files}):\n"
        f"    open(os.path.join(d, f'f{{i}}'), 'wb').write(b'x'*{size}); time.sleep({delay})\n"
    )
    return [sys.executable, "-c", code, str(dest)]


def test_watchdog_stops_clone_when_byte_quota_is_exceeded(tmp_path):
    dest = tmp_path / "ws"
    started = time.monotonic()
    with pytest.raises(rs.RepositoryError) as exc:
        rs.clone_with_limits(REF, dest, timeout=30, max_bytes=200_000, max_files=10_000,
                             command=_writer(dest, files=500, size=50_000))
    assert exc.value.code == "too_large"
    # Stopped during the clone, long before the writer would have finished (~10 s).
    assert time.monotonic() - started < 5
    used, _ = rs.measure_tree(dest)
    assert used < 200_000 + 50_000 * 20  # overshoot bounded by one polling interval of writes


def test_watchdog_stops_clone_when_file_count_is_exceeded(tmp_path):
    dest = tmp_path / "ws"
    with pytest.raises(rs.RepositoryError) as exc:
        rs.clone_with_limits(REF, dest, timeout=30, max_bytes=10**9, max_files=50,
                             command=_writer(dest, files=5_000, size=1, delay=0.001))
    assert exc.value.code == "too_many_files"


def test_clone_timeout_kills_process(tmp_path):
    with pytest.raises(rs.RepositoryError) as exc:
        rs.clone_with_limits(REF, tmp_path / "ws", timeout=0.5,
                             command=[sys.executable, "-c", "import time; time.sleep(30)"])
    assert exc.value.code == "timeout"


def test_per_file_size_limit_applies_to_child(tmp_path, monkeypatch):
    """RLIMIT_FSIZE stops a native writer (as it would git) at the per-file cap."""
    monkeypatch.setattr(rs, "MAX_FILE_BYTES", 10_000)
    dest = tmp_path / "ws"
    dest.mkdir()
    cmd = ["/bin/sh", "-c", f"head -c 50000 /dev/zero > {dest}/pack.bin"]
    with pytest.raises(rs.RepositoryError) as exc:
        rs.clone_with_limits(REF, dest, timeout=20, command=cmd)
    assert exc.value.code == "too_large"
    assert (dest / "pack.bin").stat().st_size <= 10_000


def test_discovery_finds_supported_manifests_and_skips_unsafe(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.28.0\n")
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text("{}")
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules" / "dep" / "package.json").write_text("{}")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "requirements.txt").write_text("x\n")
    (tmp_path / "big").mkdir()
    (tmp_path / "big" / "requirements.txt").write_bytes(b"a" * (rs.MAX_MANIFEST_BYTES + 1))
    outside = tmp_path.parent / "outside_secret"
    outside.mkdir(exist_ok=True)
    (outside / "package.json").write_text("{}")
    os.symlink(outside, tmp_path / "linked_dir")
    os.symlink(outside / "package.json", tmp_path / "web" / "requirements.txt")

    result = rs.discover_manifests(tmp_path)
    paths = sorted(m.relative_path for m in result.manifests)
    assert paths == ["requirements-dev.txt", "requirements.txt", "svc/pyproject.toml", "web/package.json"]
    assert any("big/requirements.txt" in s for s in result.skipped)
    assert any("web/requirements.txt" in s and "regular file" in s for s in result.skipped)


def test_discovery_depth_is_capped(tmp_path):
    deep = tmp_path
    for i in range(rs.MAX_DISCOVERY_DEPTH + 3):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    (deep / "package.json").write_text("{}")
    assert rs.discover_manifests(tmp_path).manifests == []


def test_expired_workspaces_are_cleaned(tmp_path):
    root = rs.repositories_root(tmp_path)
    old = root / "old"
    old.mkdir()
    fresh = root / "fresh"
    fresh.mkdir()
    past = time.time() - 7200
    os.utime(old, (past, past))
    assert rs.cleanup_expired_workspaces(tmp_path, ttl_seconds=3600) == 1
    assert not old.exists() and fresh.exists()
