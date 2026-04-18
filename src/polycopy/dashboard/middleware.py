"""Middleware ASGI : log structlog par requête, substitut de ``uvicorn.access``."""

from __future__ import annotations

import time

import structlog
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = structlog.get_logger(__name__)


class StructlogAccessMiddleware:
    """Logue chaque requête en JSON structlog et capture les 500 sans leak de stacktrace."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        start = time.perf_counter()
        status_holder: dict[str, int] = {"code": 0}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = int(message["status"])
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        except Exception:
            duration_ms = int((time.perf_counter() - start) * 1000)
            log.exception(
                "dashboard_request_error",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            response: Response = PlainTextResponse(
                "Internal Server Error",
                status_code=500,
            )
            await response(scope, receive, send)
            return

        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            "dashboard_request",
            method=request.method,
            path=request.url.path,
            status=status_holder["code"],
            duration_ms=duration_ms,
        )
