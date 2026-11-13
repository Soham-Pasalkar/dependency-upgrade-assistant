"""Request correlation IDs and safe server-error responses (audit F5, F6)."""
from __future__ import annotations

import contextvars
import logging
import re
import time
import uuid

from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

REQUEST_ID_HEADER = "X-Request-ID"
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

access_logger = logging.getLogger("dua.api.access")


class RequestIdFilter(logging.Filter):
    """Adds `request_id` to every log record so log lines can be correlated with responses."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """Attach a request-id aware handler to the application's loggers (idempotent)."""
    root = logging.getLogger("dua")
    if any(getattr(h, "_dua_handler", False) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler._dua_handler = True  # type: ignore[attr-defined]
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    for name in ("src",):
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        lg.setLevel(level)
        lg.propagate = False


def current_request_id() -> str:
    return request_id_var.get()


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        rid = incoming if _VALID_ID.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        started = time.monotonic()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers[REQUEST_ID_HEADER] = rid
            return response
        finally:
            access_logger.info(
                "%s %s -> %s in %.0f ms", request.method, request.url.path, status, (time.monotonic() - started) * 1000
            )
            request_id_var.reset(token)


def internal_error(logger: logging.Logger, what: str, err: BaseException) -> HTTPException:
    """Log the full error server-side and return a 500 that exposes no internal details."""
    rid = current_request_id()
    logger.exception("%s failed: %s", what, err)
    return HTTPException(status_code=500, detail=f"{what} failed because of an internal error (request {rid}).")
