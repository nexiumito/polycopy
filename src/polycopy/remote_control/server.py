"""Serveur FastAPI du ``remote_control`` (M12_bis §4.3).

Phase B — 2 routes seulement :
- ``GET /v1/health`` — liveness probe, aucune auth, zéro info sensible.
- ``GET /v1/status/<machine>`` — snapshot lecture-seule. Mismatch path
  param → ``404`` body vide (silence strict, §4.3 "404 body vide").

Invariants M12_bis :
- Aucun endpoint ``/docs``, ``/redoc``, ``/openapi.json`` exposé — copie
  stricte de l'invariant dashboard M4.5/M6.
- Préfixe versionné ``/v1`` pour permettre un futur ``/v2`` non-breaking.
- Pas de dépendance au reste de polycopy runtime (sauf ``config`` +
  ``cli.version``). L'état observable est dérivé des arguments passés
  au ``build_app`` (``boot_at`` pour l'uptime).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from polycopy.cli.version import get_version
from polycopy.remote_control.dtos import HealthResponse, StatusResponse

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)


def build_app(settings: Settings, boot_at: datetime) -> FastAPI:
    """Construit l'app FastAPI Phase B (M12_bis §4.3).

    Args:
        settings: ``Settings`` — consomme ``machine_id`` + ``execution_mode``.
        boot_at: moment du boot (UTC), utilisé pour dériver l'uptime.

    Returns:
        ``FastAPI`` prêt à être servi par uvicorn (le binding Tailscale-only
        est appliqué par ``RemoteControlOrchestrator``, pas ici).
    """
    app = FastAPI(
        title="polycopy remote_control",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    machine_id_norm = (settings.machine_id or "UNKNOWN").upper()

    @app.get("/v1/health")
    async def _health() -> HealthResponse:
        return HealthResponse(ok=True)

    @app.get("/v1/status/{machine}")
    async def _status(machine: str) -> Response:
        if machine.upper() != machine_id_norm:
            log.info(
                "remote_control_status_machine_mismatch",
                requested=machine,
                configured=machine_id_norm,
            )
            return Response(status_code=404)
        uptime_s = int((datetime.now(tz=UTC) - boot_at).total_seconds())
        payload = StatusResponse(
            machine_id=machine_id_norm,
            mode="running",
            uptime_seconds=uptime_s,
            version=get_version(),
            execution_mode=settings.execution_mode,
        ).model_dump(mode="json")
        return JSONResponse(content=payload)

    return app
