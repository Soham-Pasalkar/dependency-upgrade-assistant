"""Allow-listing CONNECT proxy for the resolver (FR-25, NFR-16; ADR-010).

Runs inside its own container from the pinned Python image, as a single read-only mounted file:
``python3 -I /opt/dua/egress_proxy.py``. Standard library only; no project imports.

Rules, applied to every TCP connection:
- exactly one request: ``CONNECT <host>:443 HTTP/1.1``; anything else (plain HTTP, other methods,
  other ports, malformed requests) gets 403 or 400;
- ``Proxy-Authorization: Basic dua:<per-job token>`` is required (407 otherwise), compared in constant
  time; the token never appears in logs;
- the host must be exactly one of the allow-listed names;
- the proxy resolves the name itself, refuses the request if ANY resolved address is not a public
  unicast address, then connects to the exact address it validated (no second lookup, so no DNS
  rebinding between check and connect);
- byte, idle-time and connection-count limits; every connection is closed at the job deadline.

Each decision is written to stdout as one JSON line (job, host, port, reason, address).
Configuration comes only from the environment set by the supervisor:
DUA_PROXY_TOKEN, DUA_PROXY_BIND, DUA_PROXY_PORT, DUA_PROXY_JOB, DUA_PROXY_DEADLINE (epoch seconds),
DUA_PROXY_ALLOW (comma-separated hosts), DUA_PROXY_MAX_BYTES.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import ipaddress
import json
import os
import socket
import sys
import time
from typing import Callable, List, Optional, Sequence, Tuple

DEFAULT_ALLOW = ("pypi.org", "files.pythonhosted.org")
MAX_HEAD_BYTES = 8192
HEAD_TIMEOUT = 10.0
CONNECT_TIMEOUT = 10.0
IDLE_TIMEOUT = 60.0
MAX_CONNECTIONS = 32
DEFAULT_MAX_BYTES = 1024 * 1024 * 1024  # per tunnel, both directions

_NAT64 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"))
_6TO4 = ipaddress.ip_network("2002::/16")
_TEREDO = ipaddress.ip_network("2001::/32")


def address_allowed(text: str) -> bool:
    """True only for public unicast addresses. Embedded/translated IPv4 forms are refused outright."""
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6:
        if ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None:
            return False
        if any(ip in net for net in _NAT64) or ip in _6TO4 or ip in _TEREDO:
            return False
    return bool(
        ip.is_global
        and not ip.is_multicast
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_reserved
        and not ip.is_unspecified
    )


class Denied(Exception):
    def __init__(self, status: int, reason: str, host: str = "", port: int = 0) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.host = host
        self.port = port


def parse_request(head: bytes) -> Tuple[str, int, dict]:
    """Parse a proxy request head. Returns (host, port, headers) for a well-formed CONNECT."""
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError:
        raise Denied(400, "non_ascii_request")
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise Denied(400, "malformed_request_line")
    method, target, _ = parts
    if method != "CONNECT":
        raise Denied(403, "method_not_allowed")
    host, sep, port_text = target.rpartition(":")
    if not sep or not port_text.isdigit() or not host or host.startswith("["):
        raise Denied(400, "malformed_target")
    port = int(port_text)
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise Denied(400, "malformed_header")
        headers[name.strip().lower()] = value.strip()
    return host.lower(), port, headers


def check_request(host: str, port: int, headers: dict, token: str, allow: Sequence[str]) -> None:
    supplied = headers.get("proxy-authorization", "")
    expected = "Basic " + base64.b64encode(f"dua:{token}".encode()).decode()
    if not token or not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise Denied(407, "proxy_auth_failed", host, port)
    if port != 443:
        raise Denied(403, "port_not_allowed", host, port)
    if host not in allow:
        raise Denied(403, "host_not_allowed", host, port)


Resolver = Callable[[str, int], List[str]]


def system_resolve(host: str, port: int) -> List[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(info[4][0] for info in infos))


def choose_address(host: str, port: int, resolve: Resolver, allowed: Callable[[str], bool] = address_allowed) -> str:
    try:
        addresses = resolve(host, port)
    except OSError:
        raise Denied(502, "dns_failed", host, port)
    if not addresses:
        raise Denied(502, "dns_empty", host, port)
    bad = [a for a in addresses if not allowed(a)]
    if bad:
        raise Denied(403, "non_public_address", host, port)
    # Prefer IPv4: Docker bridges commonly have no IPv6 route.
    return sorted(addresses, key=lambda a: ":" in a)[0]


class Proxy:
    def __init__(
        self,
        token: str,
        job: str,
        allow: Sequence[str] = DEFAULT_ALLOW,
        deadline: Optional[float] = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        resolve: Resolver = system_resolve,
        allowed: Callable[[str], bool] = address_allowed,
        log: Callable[[dict], None] = None,
        clock: Callable[[], float] = time.time,
        opener=None,
    ) -> None:
        self.token = token
        self.job = job
        self.allow = tuple(h.lower() for h in allow)
        self.deadline = deadline
        self.max_bytes = max_bytes
        self.resolve = resolve
        self.allowed = allowed
        self.log = log or (lambda event: print(json.dumps(event, sort_keys=True), flush=True))
        self.clock = clock
        self.opener = opener or asyncio.open_connection  # test seam; receives the validated address
        self.active = 0
        self.tasks: set = set()

    def _event(self, decision: str, host: str, port: int, reason: str, address: str = "") -> None:
        self.log({"job": self.job, "decision": decision, "host": host[:255], "port": port, "reason": reason,
                  "address": address})

    def remaining(self) -> Optional[float]:
        return None if self.deadline is None else self.deadline - self.clock()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self.tasks.add(task)
        self.active += 1
        try:
            await self._handle(reader, writer)
        finally:
            self.active -= 1
            self.tasks.discard(task)
            writer.close()

    async def _handle(self, reader, writer) -> None:
        host, port = "", 0
        try:
            remaining = self.remaining()
            if remaining is not None and remaining <= 0:
                raise Denied(503, "job_deadline_passed")
            if self.active > MAX_CONNECTIONS:
                raise Denied(503, "too_many_connections")
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
            except asyncio.LimitOverrunError:
                raise Denied(431, "request_head_too_large")
            except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                raise Denied(400, "incomplete_request")
            host, port, headers = parse_request(head[:-4])
            check_request(host, port, headers, self.token, self.allow)
            loop = asyncio.get_running_loop()
            address = await loop.run_in_executor(None, choose_address, host, port, self.resolve, self.allowed)
            try:
                up_reader, up_writer = await asyncio.wait_for(self.opener(address, port), CONNECT_TIMEOUT)
            except (OSError, asyncio.TimeoutError):
                raise Denied(502, "upstream_connect_failed", host, port)
        except Denied as denied:
            self._event("deny", denied.host or host, denied.port or port, denied.reason)
            try:
                writer.write(f"HTTP/1.1 {denied.status} {denied.reason}\r\nConnection: close\r\n\r\n".encode())
                await writer.drain()
            except (ConnectionError, RuntimeError):
                pass
            return
        self._event("allow", host, port, "allowed", address)
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()
        budget = [self.max_bytes]
        try:
            await self._splice(reader, writer, up_reader, up_writer, budget, host, port)
        finally:
            up_writer.close()

    async def _splice(self, reader, writer, up_reader, up_writer, budget, host, port) -> None:
        async def pipe(src, dst):
            while True:
                remaining = self.remaining()
                timeout = IDLE_TIMEOUT if remaining is None else max(0.0, min(IDLE_TIMEOUT, remaining))
                if timeout <= 0:
                    return "deadline"
                try:
                    chunk = await asyncio.wait_for(src.read(65536), timeout)
                except asyncio.TimeoutError:
                    return "deadline" if remaining is not None and remaining <= IDLE_TIMEOUT else "idle_timeout"
                except ConnectionError:
                    return "closed"
                if not chunk:
                    return "closed"
                budget[0] -= len(chunk)
                if budget[0] < 0:
                    return "byte_limit"
                dst.write(chunk)
                try:
                    await dst.drain()
                except ConnectionError:
                    return "closed"

        tasks = [asyncio.ensure_future(pipe(reader, up_writer)), asyncio.ensure_future(pipe(up_reader, writer))]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        reason = next(iter(done)).result()
        if reason in ("byte_limit", "deadline", "idle_timeout"):
            self._event("close", host, port, reason)


async def serve(proxy: Proxy, bind: str, port: int) -> None:
    server = await asyncio.start_server(proxy.handle, bind, port, limit=MAX_HEAD_BYTES)
    async with server:
        if proxy.deadline is None:
            await server.serve_forever()
        else:
            await asyncio.sleep(max(0.0, proxy.deadline - time.time()))


def main() -> int:
    token = os.environ.get("DUA_PROXY_TOKEN", "")
    bind = os.environ.get("DUA_PROXY_BIND", "")
    if len(token) < 32 or not bind:
        print(json.dumps({"decision": "error", "reason": "missing_configuration"}), flush=True)
        return 2
    allow = [h for h in os.environ.get("DUA_PROXY_ALLOW", ",".join(DEFAULT_ALLOW)).split(",") if h]
    deadline_text = os.environ.get("DUA_PROXY_DEADLINE")
    proxy = Proxy(
        token=token,
        job=os.environ.get("DUA_PROXY_JOB", ""),
        allow=allow,
        deadline=float(deadline_text) if deadline_text else None,
        max_bytes=int(os.environ.get("DUA_PROXY_MAX_BYTES", DEFAULT_MAX_BYTES)),
    )
    print(json.dumps({"decision": "ready", "job": proxy.job, "bind": bind}), flush=True)
    asyncio.run(serve(proxy, bind, int(os.environ.get("DUA_PROXY_PORT", "3128"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
