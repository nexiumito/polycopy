"""Orchestrateur Dashboard (M4.5, étendu M12_bis Phase E §4.7).

M12_bis : si ``DASHBOARD_BIND_TAILSCALE=true``, bind sur l'IP Tailscale
(résolue via ``remote_control.resolve_tailscale_ipv4``) au lieu de
``DASHBOARD_HOST``. Crash boot si Tailscale absent — fail-fast cohérent
avec ``RemoteControlOrchestrator`` Phase B.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.dashboard.routes import build_app
from polycopy.dashboard.server import serve_dashboard
from polycopy.remote_control import resolve_tailscale_ipv4

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)


class DashboardOrchestrator:
    """Sert le dashboard FastAPI tant que ``stop_event`` n'est pas déclenché."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        # M12_bis Phase E §4.7 : résolution Tailscale IP AVANT le TaskGroup
        # (même pattern fail-fast que RemoteControlOrchestrator Phase B).
        # Si Tailscale absent/down, `RemoteControlBootError` remonte ici
        # et crashe le process clair.
        self._host: str = self._resolve_host()
        self._log_cohabitation_warnings()

    def _resolve_host(self) -> str:
        """Retourne l'hôte bind : Tailscale IP (si flag on) ou DASHBOARD_HOST."""
        if self._settings.dashboard_bind_tailscale:
            return resolve_tailscale_ipv4(self._settings)
        return self._settings.dashboard_host

    def _log_cohabitation_warnings(self) -> None:
        """Warnings M12_bis §4.7 pour les configs ambiguës."""
        if not self._settings.dashboard_bind_tailscale:
            return
        if not self._settings.dashboard_enabled:
            log.warning(
                "dashboard_bind_tailscale_without_enabled_noop",
                message=(
                    "DASHBOARD_BIND_TAILSCALE=true mais DASHBOARD_ENABLED=false — "
                    "le flag est sans effet (le dashboard n'est pas instancié)."
                ),
            )
        if self._settings.dashboard_host != "127.0.0.1":
            log.warning(
                "dashboard_host_overridden_by_tailscale_bind",
                message=(
                    "DASHBOARD_HOST défini explicitement mais DASHBOARD_BIND_TAILSCALE=true. "
                    f"L'IP Tailscale ({self._host}) prend priorité — DASHBOARD_HOST "
                    f"({self._settings.dashboard_host}) est ignoré."
                ),
                tailscale_host=self._host,
                ignored_dashboard_host=self._settings.dashboard_host,
            )

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle principale : démarre uvicorn et attend ``stop_event.set()``."""
        log.info(
            "dashboard_starting",
            host=self._host,
            port=self._settings.dashboard_port,
            bind_tailscale=self._settings.dashboard_bind_tailscale,
        )
        app = build_app(self._session_factory, self._settings)
        log.info(
            "dashboard_started",
            host=self._host,
            port=self._settings.dashboard_port,
        )
        try:
            await serve_dashboard(
                app,
                host=self._host,
                port=self._settings.dashboard_port,
                stop_event=stop_event,
            )
        finally:
            log.info("dashboard_stopped")
