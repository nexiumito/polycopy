"""Orchestrateur du ``remote_control`` — entrée TaskGroup ``cli/runner.py`` (M12_bis §4.5).

Pattern strict copy-paste de ``DashboardOrchestrator`` ([dashboard/server.py](
../dashboard/server.py)) :
- ``uvicorn.Config`` + ``uvicorn.Server``.
- Signal handlers uvicorn désactivés → délégation à ``cli/runner.py``.
- Watchdog ``stop_event`` pose ``server.should_exit = True``.

Invariants M12_bis :
- Bind **strictement** sur l'IP Tailscale retournée par
  ``resolve_tailscale_ipv4`` (jamais ``0.0.0.0`` ni ``127.0.0.1``).
- Boot fatal si Tailscale indisponible — ``RemoteControlBootError``
  remonte à ``cli/runner.py`` et crashe le process avec exit code 1.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
import uvicorn

from polycopy.remote_control.server import build_app
from polycopy.remote_control.tailscale import resolve_tailscale_ipv4

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)


def _disable_uvicorn_signal_handlers(server: uvicorn.Server) -> None:
    """Empêche uvicorn de remplacer les handlers SIGINT/SIGTERM du runner."""

    def _noop() -> None:
        return None

    setattr(server, "install_signal_handlers", _noop)  # noqa: B010


class RemoteControlOrchestrator:
    """Sert l'API ``remote_control`` tant que ``stop_event`` n'est pas déclenché.

    Instancié conditionnellement par ``cli/runner.py`` si
    ``settings.remote_control_enabled`` est ``True``. La résolution Tailscale
    est appelée au __init__ (avant TaskGroup) pour que l'erreur remonte en
    clair si Tailscale est down — pas silencieusement dans le TaskGroup.
    """

    def __init__(self, settings: Settings, boot_at: datetime | None = None) -> None:
        self._settings = settings
        self._boot_at = boot_at if boot_at is not None else datetime.now(tz=UTC)
        self._host = resolve_tailscale_ipv4(settings)
        self._port = settings.remote_control_port
        log.info(
            "remote_control_init",
            host=self._host,
            port=self._port,
            machine_id=(settings.machine_id or "UNKNOWN").upper(),
        )

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle principale : démarre uvicorn jusqu'à ``stop_event.set()``."""
        app = build_app(self._settings, self._boot_at)
        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_config=None,
            access_log=False,
            lifespan="on",
        )
        server = uvicorn.Server(config)
        _disable_uvicorn_signal_handlers(server)

        log.info("remote_control_started", host=self._host, port=self._port)

        async def _watchdog() -> None:
            await stop_event.wait()
            server.should_exit = True

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(server.serve())
                tg.create_task(_watchdog())
        finally:
            log.info("remote_control_stopped")
