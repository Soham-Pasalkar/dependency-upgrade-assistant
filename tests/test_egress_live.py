"""Live egress isolation from inside a resolver container built by the production runtime (criterion 8).

Real Docker networks, the real proxy container and real DNS. Runs in the CI Docker job.
"""
from __future__ import annotations

import socket
import threading
import time
import uuid

import pytest

from tests.live_resolution import docker, leftovers, probe, require_docker

pytestmark = pytest.mark.live_resolution

PROBE = r"""
import base64, json, socket, sys
proxy, token = sys.argv[1], sys.argv[2]
targets = sys.argv[3:]
out = {}
def tcp(host, port, timeout=4):
    try:
        socket.create_connection((host, int(port)), timeout).close()
        return "connected"
    except Exception as e:
        return type(e).__name__
out["direct_public_tcp"] = tcp("1.1.1.1", 443)
out["direct_pypi_ip_tcp"] = tcp("151.101.0.223", 443)
try:
    socket.getaddrinfo("pypi.org", 443)
    out["dns"] = "resolved"
except Exception as e:
    out["dns"] = type(e).__name__
def ask(target, auth=True, method="CONNECT", tok=None):
    try:
        s = socket.create_connection((proxy, 3128), 5)
    except Exception as e:
        return "unreachable:" + type(e).__name__
    head = f"{method} {target} HTTP/1.1\r\nHost: {target}\r\n"
    if auth:
        head += "Proxy-Authorization: Basic " + base64.b64encode(f"dua:{tok or token}".encode()).decode() + "\r\n"
    s.sendall((head + "\r\n").encode())
    s.settimeout(20)
    line = s.recv(200).split(b"\r\n")[0].decode()
    s.close()
    return line
out["pypi"] = ask("pypi.org:443")
out["files"] = ask("files.pythonhosted.org:443")
out["other_host"] = ask("example.com:443")
out["metadata_ip"] = ask("169.254.169.254:443")
out["metadata_name"] = ask("metadata.google.internal:443")
out["ssh_port"] = ask("pypi.org:22")
out["plain_http"] = ask("http://pypi.org/simple/", method="GET")
out["no_auth"] = ask("pypi.org:443", auth=False)
out["wrong_token"] = ask("pypi.org:443", tok="x" * 43)
for t in targets:
    host, port = t.rsplit(":", 1)
    out["tcp " + t] = tcp(host, port, 3)
print(json.dumps(out))
"""

REACH = r"""
import json, socket, sys
try:
    socket.create_connection((sys.argv[1], 3128), 4).close()
    print(json.dumps({"proxy": "connected"}))
except Exception as e:
    print(json.dumps({"proxy": type(e).__name__}))
"""


def _host_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 53))
        return s.getsockname()[0]
    finally:
        s.close()


@pytest.fixture(scope="module")
def isolated():
    rt = require_docker()
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def accept():
        listener.settimeout(0.5)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
                conn.close()
            except OSError:
                pass
    threading.Thread(target=accept, daemon=True).start()
    job = uuid.uuid4().hex
    net = rt.start_job(job, time.time() + 600)
    try:
        gateway = net.subnet.rsplit(".", 1)[0] + "." + str(int(net.subnet.rsplit(".", 1)[1].split("/")[0]) + 1)
        targets = [f"{gateway}:{port}", f"172.17.0.1:{port}", f"{_host_ip()}:{port}", f"127.0.0.1:{port}"]
        result = probe(rt, net, PROBE, net.proxy_ip, net.token, *targets)
        yield rt, net, result, targets, job
    finally:
        rt.stop_job(net)
        stop.set()
        listener.close()


def test_listener_is_reachable_from_the_host_itself(isolated):
    """Control: the host listener works, so 'unreachable' below is caused by isolation."""
    _, _, _, targets, _ = isolated
    host, port = targets[2].rsplit(":", 1)
    socket.create_connection((host, int(port)), 3).close()


def test_no_direct_network_and_no_dns(isolated):
    _, _, result, _, _ = isolated
    assert result["direct_public_tcp"] != "connected"
    assert result["direct_pypi_ip_tcp"] != "connected"
    assert result["dns"] != "resolved"


def test_host_gateway_and_lan_are_unreachable(isolated):
    _, _, result, targets, _ = isolated
    for t in targets:
        assert result["tcp " + t] != "connected", t


def test_allow_listed_registries_are_reachable_through_the_proxy(isolated):
    _, _, result, _, _ = isolated
    assert result["pypi"].startswith("HTTP/1.1 200") and result["files"].startswith("HTTP/1.1 200")


@pytest.mark.parametrize("key,status", [
    ("other_host", "403"), ("metadata_ip", "403"), ("metadata_name", "403"), ("ssh_port", "403"),
    ("plain_http", "403"), ("no_auth", "407"), ("wrong_token", "407"),
])
def test_proxy_refuses_everything_else(isolated, key, status):
    _, _, result, _, _ = isolated
    assert result[key].split(" ")[1] == status, result[key]


def test_denials_are_logged_without_the_token(isolated):
    rt, net, _, _, _ = isolated
    events = rt.proxy_events(net)
    reasons = {(e["host"], e["reason"]) for e in events}
    assert ("example.com", "host_not_allowed") in reasons and ("pypi.org", "port_not_allowed") in reasons
    assert net.token not in docker("logs", net.proxy_container).stdout


def test_proxy_is_unreachable_from_other_networks(isolated):
    rt, net, _, _, _ = isolated
    assert probe(rt, net, REACH, net.proxy_ip, network="bridge")["proxy"] != "connected"
    other = rt.start_job(uuid.uuid4().hex, time.time() + 300)
    try:
        assert probe(rt, other, REACH, net.proxy_ip)["proxy"] != "connected"
    finally:
        rt.stop_job(other)


def test_job_resources_are_removed(isolated):
    rt, net, _, _, job = isolated
    other_job = uuid.uuid4().hex
    other = rt.start_job(other_job, time.time() + 300)
    assert leftovers(other_job)
    rt.stop_job(other)
    assert leftovers(other_job) == []
