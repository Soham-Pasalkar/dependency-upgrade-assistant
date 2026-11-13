"""The supervisor's PyPI client and target facts (TB-R4; ADR-011).

Transport tests use a local HTTP server on 127.0.0.1 (no public network). Client tests use recorded
responses through the http_get seam; URL checks and limits are the real ones.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.resolution import target
from src.resolution.fingerprint import ResolutionEnvironmentError, resolution_environment
from src.resolution.metadata_client import (
    Deadline,
    DeadlineExceeded,
    MetadataFetchError,
    PyPIMetadataClient,
    check_url,
    project_slug,
    requests_http_get,
)
from src.sandbox.image_catalog import PINNED_IMAGE_DIGESTS
from tests.resolution_fixtures import FakePyPI, client

# -- URL and name allow-listing ---------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "http://pypi.org/simple/x/",
    "https://evil.example/simple/x/",
    "https://pypi.org.evil.example/simple/x/",
    "https://user:pw@pypi.org/simple/x/",
    "https://pypi.org:8443/simple/x/",
    "https://pypi.org/simple/x/?redirect=1",
    "https://169.254.169.254/latest/meta-data/",
    "https://[::1]/simple/x/",
    "file:///etc/passwd",
])
def test_only_https_on_the_fixed_host(url):
    with pytest.raises(MetadataFetchError) as err:
        check_url(url, "pypi.org")
    assert err.value.code == "host_not_allowed"


def test_fragment_is_dropped():
    assert check_url("https://pypi.org/simple/x/#sha256=1", "pypi.org") == "https://pypi.org/simple/x/"


@pytest.mark.parametrize("name", ["../etc", "a/b", "a b", "", "a%2fb", "-a"])
def test_project_names_cannot_inject_paths(name):
    with pytest.raises(MetadataFetchError):
        project_slug(name)


def test_index_with_file_on_another_host_is_rejected():
    fake = FakePyPI()
    fake.edit_index_file("blinker", "blinker-1.9.0-py3-none-any.whl",
                         lambda e: e.__setitem__("url", "https://evil.example/blinker-1.9.0-py3-none-any.whl"))
    with pytest.raises(MetadataFetchError) as err:
        client(fake).project_index("blinker", Deadline())
    assert err.value.code == "host_not_allowed"


def test_metadata_is_only_fetched_from_files_host():
    with pytest.raises(MetadataFetchError):
        client().metadata_file("https://pypi.org/packages/x.whl", "0" * 64, Deadline())


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("meta"),
    lambda d: d.__setitem__("meta", {"api-version": "2.0"}),
    lambda d: d.__setitem__("name", "other"),
    lambda d: d.__setitem__("files", {}),
    lambda d: d["files"].append("x"),
    lambda d: d["files"][0].__setitem__("filename", "../x.whl"),
    lambda d: d["files"][0].pop("hashes"),
    lambda d: d["files"][0].__setitem__("yanked", 1),
])
def test_malformed_index_is_rejected(mutate):
    fake = FakePyPI()
    data = fake.index("blinker")
    mutate(data)
    fake.set_index("blinker", data)
    with pytest.raises(MetadataFetchError):
        client(fake).project_index("blinker", Deadline())


@pytest.mark.parametrize("status,code", [(301, "redirect"), (302, "redirect"), (404, "not_found"),
                                         (500, "http_status")])
def test_non_200_answers(status, code):
    fake = FakePyPI()
    fake.overrides["https://pypi.org/simple/blinker/"] = (status, b"")
    with pytest.raises(MetadataFetchError) as err:
        client(fake).project_index("blinker", Deadline())
    assert err.value.code == code


def test_requests_are_bounded_and_cached():
    fake = FakePyPI()
    c = client(fake)
    c.project_index("Blinker", Deadline())
    c.project_index("blinker", Deadline())
    assert len(fake.calls) == 1
    url, timeout, max_bytes = fake.calls[0]
    assert url == "https://pypi.org/simple/blinker/" and timeout == 8.0 and max_bytes == 16 * 1024 * 1024


def test_request_timeout_is_capped_by_the_run_deadline():
    now = [100.0]
    deadline = Deadline(at=103.0, clock=lambda: now[0])
    assert deadline.request_timeout() == 3.0
    now[0] = 90.0
    assert deadline.request_timeout() == 8.0
    now[0] = 103.0
    with pytest.raises(DeadlineExceeded):
        deadline.request_timeout()


def test_metadata_hash_is_checked():
    fake = FakePyPI()
    entry = next(f for f in fake.index("blinker")["files"] if f["filename"].endswith(".whl"))
    c = client(fake)
    assert c.metadata_file(entry["url"], entry["core-metadata"]["sha256"], Deadline())
    with pytest.raises(MetadataFetchError) as err:
        c.metadata_file(entry["url"], "0" * 64, Deadline())
    assert err.value.code == "hash_mismatch"


def test_version_json_validates_inputs_and_shape():
    fake = FakePyPI()
    c = client(fake)
    assert c.version_json("blinker", "1.9.0", Deadline()).endpoint == "https://pypi.org/pypi/blinker/1.9.0/json"
    with pytest.raises(MetadataFetchError):
        c.version_json("blinker", "1.9.0/../../x", Deadline())
    fake.overrides["https://pypi.org/pypi/blinker/1.8.0/json"] = (200, json.dumps({"info": {}}).encode())
    with pytest.raises(MetadataFetchError):
        c.version_json("blinker", "1.8.0", Deadline())


# -- real transport against a local server ----------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/big-declared":
            self.send_response(200)
            self.send_header("Content-Length", str(10_000))
            self.end_headers()
            self.wfile.write(b"x" * 10_000)
        elif self.path == "/big-streamed":
            self.send_response(200)
            self.end_headers()
            for _ in range(10):
                self.wfile.write(b"x" * 1000)
        elif self.path == "/slow":
            time.sleep(1.5)
            self.send_response(200)
            self.end_headers()
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/x")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_transport_reads_small_bodies(server):
    assert requests_http_get(server + "/ok", {}, 5, 1000) == (200, b'{"ok": true}')


@pytest.mark.parametrize("path", ["/big-declared", "/big-streamed"])
def test_transport_enforces_size_limit(server, path):
    with pytest.raises(MetadataFetchError) as err:
        requests_http_get(server + path, {}, 5, 5000)
    assert err.value.code == "too_large"


def test_transport_times_out(server):
    with pytest.raises(MetadataFetchError) as err:
        requests_http_get(server + "/slow", {}, 0.3, 1000)
    assert err.value.code == "timeout"


def test_transport_does_not_follow_redirects(server):
    status, body = requests_http_get(server + "/redirect", {}, 5, 1000)
    assert (status, body) == (302, b"")


def test_client_turns_transport_redirect_into_error(server):
    def via_server(url, headers, timeout, max_bytes):
        return requests_http_get(server + "/redirect", headers, timeout, max_bytes)

    with pytest.raises(MetadataFetchError) as err:
        PyPIMetadataClient(http_get=via_server).project_index("blinker", Deadline())
    assert err.value.code == "redirect"


def test_transport_unreachable_is_network_error():
    with pytest.raises(MetadataFetchError) as err:
        requests_http_get("http://127.0.0.1:1/x", {}, 2, 1000)
    assert err.value.code in ("network", "timeout")


# -- trusted target facts -----------------------------------------------------------------------


def test_target_facts_belong_to_the_pinned_image():
    assert PINNED_IMAGE_DIGESTS["python:3.11-slim"] == target.TARGET_FACTS_IMAGE_DIGEST


def test_other_image_fails_closed():
    env = resolution_environment("amd64", image="python:3.11-slim@sha256:" + "1" * 64)
    with pytest.raises(ResolutionEnvironmentError):
        target.marker_environment(env)
    with pytest.raises(ResolutionEnvironmentError):
        target.supported_tags(resolution_environment("s390x"))


@pytest.mark.parametrize("arch,machine", [("amd64", "x86_64"), ("arm64", "aarch64")])
def test_marker_environment(arch, machine):
    env = target.marker_environment(resolution_environment(arch))
    assert env["platform_machine"] == machine and env["python_full_version"] == "3.11.18"
    assert env["sys_platform"] == "linux" and "platform_release" not in env


def test_supported_tags():
    from packaging.utils import parse_wheel_filename
    tags = target.supported_tags(resolution_environment("amd64"))

    def ok(filename):
        return bool(parse_wheel_filename(filename)[3] & tags)

    assert ok("numpy-1.24.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl")
    assert ok("x-1-cp38-abi3-manylinux_2_28_x86_64.whl") and ok("x-1-py3-none-any.whl")
    assert ok("x-1-cp311-cp311-manylinux_2_41_x86_64.whl")
    assert not ok("x-1-cp311-cp311-manylinux_2_42_x86_64.whl")  # newer glibc than the image
    assert not ok("x-1-cp310-cp310-manylinux_2_17_x86_64.whl")
    assert not ok("x-1-cp311-cp311-win_amd64.whl") and not ok("x-1-cp311-cp311-macosx_11_0_arm64.whl")
    assert not ok("x-1-cp311-cp311-manylinux_2_17_aarch64.whl")
    assert not ok("x-1-cp311-cp311-musllinux_1_2_x86_64.whl")
