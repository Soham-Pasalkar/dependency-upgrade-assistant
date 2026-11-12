"""Egress proxy decision logic and local behaviour (FR-25; ADR-010; M12 criterion 8, unit part).

These run on 127.0.0.1 with injected DNS answers and an injected upstream connector. They do not prove
container network isolation; that is tests/test_egress_live.py in the CI Docker job.
"""
from __future__ import annotations

import asyncio
import base64

import pytest

from src.resolution import egress_proxy as ep

TOKEN = "t" * 40
AUTH = "Proxy-Authorization: Basic " + base64.b64encode(f"dua:{TOKEN}".encode()).decode()


@pytest.mark.parametrize("address,ok", [
    ("151.101.0.223", True), ("2a04:4e42::223", True), ("8.8.8.8", True),
    ("10.0.0.1", False), ("172.16.5.4", False), ("192.168.1.1", False), ("127.0.0.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),
    ("255.255.255.255", False), ("198.18.0.1", False), ("192.0.2.1", False), ("240.0.0.1", False),
    ("::1", False), ("::", False), ("fe80::1", False), ("fe80::1%eth0", False), ("fc00::1", False),
    ("fd12:3456::1", False), ("ff02::1", False), ("::ffff:10.0.0.1", False), ("::ffff:8.8.8.8", False),
    ("64:ff9b::808:808", False), ("64:ff9b:1::1", False), ("2002:a00::1", False), ("2001:0:4136::1", False),
    ("2001:db8::1", False), ("not-an-ip", False), ("", False),
])
def test_only_public_unicast_addresses(address, ok):
    assert ep.address_allowed(address) is ok


@pytest.mark.parametrize("head,status,reason", [
    (b"GET http://pypi.org/ HTTP/1.1", 403, "method_not_allowed"),
    (b"POST pypi.org:443 HTTP/1.1", 403, "method_not_allowed"),
    (b"CONNECT pypi.org HTTP/1.1", 400, "malformed_target"),
    (b"CONNECT [::1]:443 HTTP/1.1", 400, "malformed_target"),
    (b"CONNECT pypi.org:443", 400, "malformed_request_line"),
    (b"CONNECT pypi.org:443 HTTP/1.1\r\nbad header", 400, "malformed_header"),
    ("CONNECT pypí.org:443 HTTP/1.1".encode(), 400, "non_ascii_request"),
])
def test_malformed_or_non_connect_requests(head, status, reason):
    with pytest.raises(ep.Denied) as err:
        ep.parse_request(head)
    assert (err.value.status, err.value.reason) == (status, reason)


def check(host="pypi.org", port=443, auth=AUTH, token=TOKEN):
    head = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}" + (f"\r\n{auth}" if auth else "")
    h, p, headers = ep.parse_request(head.encode())
    ep.check_request(h, p, headers, token, ep.DEFAULT_ALLOW)
    return h


def test_allowed_request():
    assert check() == "pypi.org" and check("FILES.pythonhosted.org") == "files.pythonhosted.org"


@pytest.mark.parametrize("kwargs,status,reason", [
    (dict(auth=None), 407, "proxy_auth_failed"),
    (dict(auth="Proxy-Authorization: Basic " + base64.b64encode(b"dua:other-job-token-xxxxxxxxxxxxxxxxx").decode()),
     407, "proxy_auth_failed"),
    (dict(auth=AUTH.replace("Basic", "Bearer")), 407, "proxy_auth_failed"),
    (dict(token=""), 407, "proxy_auth_failed"),
    (dict(port=80), 403, "port_not_allowed"),
    (dict(port=22), 403, "port_not_allowed"),
    (dict(host="evil.example"), 403, "host_not_allowed"),
    (dict(host="pypi.org.evil.example"), 403, "host_not_allowed"),
    (dict(host="pypi.org."), 403, "host_not_allowed"),
    (dict(host="169.254.169.254"), 403, "host_not_allowed"),
    (dict(host="registry.npmjs.org"), 403, "host_not_allowed"),
])
def test_denied_requests(kwargs, status, reason):
    with pytest.raises(ep.Denied) as err:
        check(**kwargs)
    assert (err.value.status, err.value.reason) == (status, reason)


def test_any_non_public_answer_denies_the_whole_request():
    with pytest.raises(ep.Denied) as err:
        ep.choose_address("pypi.org", 443, lambda h, p: ["151.101.0.223", "10.0.0.5"])
    assert err.value.reason == "non_public_address"


def test_dns_failures():
    def boom(h, p):
        raise OSError("nxdomain")
    with pytest.raises(ep.Denied) as err:
        ep.choose_address("pypi.org", 443, boom)
    assert err.value.reason == "dns_failed"
    with pytest.raises(ep.Denied):
        ep.choose_address("pypi.org", 443, lambda h, p: [])


def test_ipv4_preferred():
    assert ep.choose_address("pypi.org", 443, lambda h, p: ["2a04:4e42::223", "151.101.0.223"]) == "151.101.0.223"


# -- the asyncio server on 127.0.0.1 --------------------------------------------------------------


class Harness:
    def __init__(self, answers=("151.101.0.223",), **kw):
        self.events, self.resolved, self.opened = [], [], []
        self.answers = list(answers)
        self.echo_port = None

        def resolve(host, port):
            self.resolved.append(host)
            return list(self.answers)

        async def opener(address, port):
            self.opened.append((address, port))
            return await asyncio.open_connection("127.0.0.1", self.echo_port)

        self.proxy = ep.Proxy(TOKEN, "job-1", resolve=resolve, opener=opener, log=self.events.append, **kw)

    async def start(self):
        async def echo(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
        self.echo = await asyncio.start_server(echo, "127.0.0.1", 0)
        self.echo_port = self.echo.sockets[0].getsockname()[1]
        self.server = await asyncio.start_server(self.proxy.handle, "127.0.0.1", 0, limit=ep.MAX_HEAD_BYTES)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        self.echo.close()


async def request(port, head: str, payload: bytes = b"", read_timeout=2.0):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode() + b"\r\n\r\n")
    await writer.drain()
    status = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), read_timeout)
    echoed = b""
    if payload:
        writer.write(payload)
        await writer.drain()
        try:
            while len(echoed) < len(payload):
                chunk = await asyncio.wait_for(reader.read(65536), read_timeout)
                if not chunk:
                    break
                echoed += chunk
        except asyncio.TimeoutError:
            pass
    writer.close()
    return status.split(b"\r\n")[0].decode(), echoed


def run(coro):
    return asyncio.run(coro)


def test_allowed_tunnel_uses_the_validated_address():
    async def scenario():
        h = Harness(answers=["151.101.0.223"])
        port = await h.start()
        status, echoed = await request(port, f"CONNECT pypi.org:443 HTTP/1.1\r\n{AUTH}", b"hello")
        await h.stop()
        return h, status, echoed
    h, status, echoed = run(scenario())
    assert status.startswith("HTTP/1.1 200") and echoed == b"hello"
    assert h.resolved == ["pypi.org"] and h.opened == [("151.101.0.223", 443)]
    assert h.events[-1]["decision"] == "allow" and h.events[-1]["address"] == "151.101.0.223"


@pytest.mark.parametrize("head,answers,expected,reason", [
    ("CONNECT pypi.org:443 HTTP/1.1", ["151.101.0.223"], "407", "proxy_auth_failed"),
    (f"CONNECT evil.example:443 HTTP/1.1\r\n{AUTH}", ["151.101.0.223"], "403", "host_not_allowed"),
    (f"CONNECT pypi.org:443 HTTP/1.1\r\n{AUTH}", ["127.0.0.1"], "403", "non_public_address"),
    (f"CONNECT pypi.org:443 HTTP/1.1\r\n{AUTH}", ["169.254.169.254"], "403", "non_public_address"),
    (f"GET http://pypi.org/simple/ HTTP/1.1\r\n{AUTH}", ["151.101.0.223"], "403", "method_not_allowed"),
])
def test_denials_never_open_an_upstream_and_never_log_the_token(head, answers, expected, reason):
    async def scenario():
        h = Harness(answers=answers)
        port = await h.start()
        status, _ = await request(port, head)
        await h.stop()
        return h, status
    h, status = run(scenario())
    assert status.split(" ")[1] == expected
    assert h.opened == [] and h.events[-1]["reason"] == reason and h.events[-1]["decision"] == "deny"
    assert TOKEN not in repr(h.events)


def test_byte_limit_closes_the_tunnel():
    async def scenario():
        h = Harness(max_bytes=10)
        port = await h.start()
        status, echoed = await request(port, f"CONNECT pypi.org:443 HTTP/1.1\r\n{AUTH}", b"x" * 100, 1.0)
        await asyncio.sleep(0.05)
        await h.stop()
        return h, status, echoed
    h, status, echoed = run(scenario())
    assert status.startswith("HTTP/1.1 200") and len(echoed) < 100
    assert h.events[-1]["reason"] == "byte_limit"


def test_requests_after_the_job_deadline_are_refused():
    async def scenario():
        h = Harness(deadline=0.0)
        port = await h.start()
        status, _ = await request(port, f"CONNECT pypi.org:443 HTTP/1.1\r\n{AUTH}")
        await h.stop()
        return h, status
    h, status = run(scenario())
    assert status.split(" ")[1] == "503" and h.events[-1]["reason"] == "job_deadline_passed" and h.opened == []


def test_open_tunnel_is_closed_at_the_job_deadline():
    import time as _time

    async def scenario():
        h = Harness(deadline=_time.time() + 0.5)
        port = await h.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT pypi.org:443 HTTP/1.1\r\n{AUTH}\r\n\r\n".encode())
        await reader.readuntil(b"\r\n\r\n")
        started = _time.time()
        data = await asyncio.wait_for(reader.read(10), 5)  # nothing sent: closes at the deadline
        elapsed = _time.time() - started
        writer.close()
        await h.stop()
        return h, data, elapsed
    h, data, elapsed = run(scenario())
    assert data == b"" and elapsed < 3 and h.events[-1]["reason"] == "deadline"


def test_oversized_request_head_is_refused():
    async def scenario():
        h = Harness()
        port = await h.start()
        status, _ = await request(port, "CONNECT pypi.org:443 HTTP/1.1\r\nX: " + "a" * 10000)
        await h.stop()
        return h, status
    h, status = run(scenario())
    assert status.split(" ")[1] == "431" and h.opened == []


def test_incomplete_request_times_out(monkeypatch):
    monkeypatch.setattr(ep, "HEAD_TIMEOUT", 0.2)

    async def scenario():
        h = Harness()
        port = await h.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT pypi.org:443 HTTP/1.1\r\n")
        line = await asyncio.wait_for(reader.readuntil(b"\r\n"), 2)
        writer.close()
        await h.stop()
        return h, line
    h, line = run(scenario())
    assert b"400" in line and h.events[-1]["reason"] == "incomplete_request"


def test_upstream_unavailable():
    async def scenario():
        h = Harness()

        async def refuse(address, port):
            raise ConnectionRefusedError()
        h.proxy.opener = refuse
        port = await h.start()
        status, _ = await request(port, f"CONNECT pypi.org:443 HTTP/1.1\r\n{AUTH}")
        await h.stop()
        return h, status
    h, status = run(scenario())
    assert status.split(" ")[1] == "502" and h.events[-1]["reason"] == "upstream_connect_failed"


def test_main_refuses_to_start_without_configuration(monkeypatch, capsys):
    monkeypatch.delenv("DUA_PROXY_TOKEN", raising=False)
    monkeypatch.setenv("DUA_PROXY_BIND", "10.0.0.2")
    assert ep.main() == 2
    monkeypatch.setenv("DUA_PROXY_TOKEN", "short")
    assert ep.main() == 2
    assert "missing_configuration" in capsys.readouterr().out


def test_proxy_module_is_standard_library_only():
    import ast
    tree = ast.parse(open(ep.__file__).read())
    modules = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    modules |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    import sys
    assert all(m in sys.stdlib_module_names for m in modules), modules
