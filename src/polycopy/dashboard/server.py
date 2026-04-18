"""Wrapper uvicorn async avec watchdog ``stop_event`` (spec §4)."""

from __future__ import annotations

import asyncio

import structlog
import uvicorn
from fastapi import FastAPI

log = structlog.get_logger(__name__)


def _disable_uvicorn_signal_handlers(server: uvicorn.Server) -> None:
    """Empêche uvicorn de remplacer les handlers installés par ``__main__``."""

    def _noop() -> None:
        return None

    # ``install_signal_handlers`` est une méthode interne d'uvicorn ; on la
    # neutralise dynamiquement — mypy ne voit pas l'attribut dans les stubs.
    setattr(server, "install_signal_handlers", _noop)  # noqa: B010


async def serve_dashboard(
    app: FastAPI,
    *,
    host: str,
    port: int,
    stop_event: asyncio.Event,
) -> None:
    """Sert ``app`` via uvicorn jusqu'à ``stop_event.set()``.

    Pattern retenu (spec §4.2) : un ``TaskGroup`` interne lance ``server.serve()``
    et un watchdog qui positionne ``server.should_exit = True`` quand
    ``stop_event`` est déclenché.
    """
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=None,
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    # uvicorn installe ses propres handlers SIGINT/SIGTERM par défaut — on délègue
    # à ``__main__`` via ``stop_event`` pour préserver l'atomicité du shutdown.
    _disable_uvicorn_signal_handlers(server)

    async def _watchdog() -> None:
        await stop_event.wait()
        server.should_exit = True

    async with asyncio.TaskGroup() as tg:
        tg.create_task(server.serve())
        tg.create_task(_watchdog())
