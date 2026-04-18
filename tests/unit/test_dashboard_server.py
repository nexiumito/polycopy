"""Test lifecycle du wrapper ``serve_dashboard`` (port=0 — peut être skippé en CI hostile)."""

from __future__ import annotations

import asyncio
import socket

import pytest
from fastapi import FastAPI

from polycopy.dashboard.server import serve_dashboard


def _loopback_available() -> bool:
    """Teste un bind loopback éphémère. Skip test si indisponible (CI hostile)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
    except OSError:
        return False
    return True


@pytest.mark.skipif(
    not _loopback_available(),
    reason="127.0.0.1 bind indisponible (CI hostile)",
)
@pytest.mark.asyncio
async def test_serve_dashboard_stops_on_stop_event() -> None:
    """``serve_dashboard`` doit sortir proprement quand ``stop_event`` est set."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def _healthz() -> dict[str, str]:
        return {"status": "ok"}

    stop_event = asyncio.Event()

    async def _trigger_stop() -> None:
        await asyncio.sleep(0.3)
        stop_event.set()

    # Lance serve + trigger en parallèle avec un timeout global.
    await asyncio.wait_for(
        asyncio.gather(
            serve_dashboard(app, host="127.0.0.1", port=0, stop_event=stop_event),
            _trigger_stop(),
        ),
        timeout=5.0,
    )
