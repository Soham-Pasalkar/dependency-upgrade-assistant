"""Request protections for a local web API (NFR-1, T-1, T-2).

- Host header must name a loopback host: blocks DNS-rebinding attacks.
- State-changing requests must come from an allowed Origin, carry ``X-DUA-Client: web`` and use
  ``application/json`` (or multipart on the upload endpoint). A cross-site form or simple
  request cannot satisfy all three, so it cannot trigger analysis, approval or export.
"""
from __future__ import annotations

from typing import Iterable, Optional, Set

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CLIENT_HEADER = "x-dua-client"
CLIENT_HEADER_VALUE = "web"
MULTIPART_PATHS = {"/api/manifest/upload", "/api/v1/manifest/upload", "/manifest/upload"}


def hostname_of(host_header: str) -> Optional[str]:
    value = (host_header or "").strip().lower()
    if not value:
        return None
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else None
    if value.count(":") == 1:
        name, port = value.split(":", 1)
        if not port.isdigit():
            return None
        return name
    if ":" in value:
        return None
    return value


class RequestProtectionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, allowed_hosts: Iterable[str], allowed_origins: Iterable[str]) -> None:
        super().__init__(app)
        self.allowed_hosts: Set[str] = {h.strip().lower().strip("[]") for h in allowed_hosts if h.strip()}
        self.allowed_origins: Set[str] = {o.strip().rstrip("/") for o in allowed_origins if o.strip()}

    async def dispatch(self, request: Request, call_next):
        host = hostname_of(request.headers.get("host", ""))
        if host is None or host not in self.allowed_hosts:
            return JSONResponse(status_code=400, content={"detail": "Invalid Host header."})

        if request.method.upper() in SAFE_METHODS:
            return await call_next(request)

        origin = (request.headers.get("origin") or "").strip().rstrip("/")
        if not origin or origin == "null" or origin not in self.allowed_origins:
            return JSONResponse(status_code=403, content={"detail": "Request blocked: origin not allowed."})

        if request.headers.get(CLIENT_HEADER, "").strip().lower() != CLIENT_HEADER_VALUE:
            return JSONResponse(status_code=403, content={"detail": "Request blocked: missing client header."})

        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = 1
        has_body = length > 0 or "chunked" in request.headers.get("transfer-encoding", "").lower()
        if has_body or content_type:
            allowed = {"application/json"}
            if request.url.path in MULTIPART_PATHS:
                allowed.add("multipart/form-data")
            if content_type not in allowed:
                return JSONResponse(status_code=415, content={"detail": "Unsupported content type."})

        return await call_next(request)
