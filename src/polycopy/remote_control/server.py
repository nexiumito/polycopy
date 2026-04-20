"""Serveur FastAPI du ``remote_control`` (M12_bis §4.3).

Routes :
- ``GET /v1/health`` (Phase B) — liveness probe, aucune auth.
- ``GET /v1/status/<machine>`` (Phase B) — snapshot lecture-seule,
  aucune auth, mismatch ⇒ 404 body vide.
- ``POST /v1/restart/<machine>`` (Phase C) — stop_event.set(), respawn
  normal via superviseur.
- ``POST /v1/stop/<machine>`` (Phase C) — sentinel.touch + stop_event,
  respawn en mode paused.
- ``POST /v1/resume/<machine>`` (Phase C) — sentinel.clear + stop_event,
  409 si non-paused.

Les routes destructives (POST) s'enregistrent seulement si toutes les
dépendances (`stop_event`, `sentinel`, `totp_guard`, `rate_limiter`,
`lockdown`) sont fournies. Phase B tests peuvent continuer à appeler
``build_app`` sans elles — seules les GET seront exposées.

Flow commun routes destructives (§4.3.3-5) :
1. Match ``<machine>`` case-insensitive — 404 body vide si mismatch.
2. Lockdown actif → 423 Locked (silencieux, aucun détail).
3. Rate limiter → 429 si dépassé.
4. TOTP verify → 401 sur échec + ``record_failure`` → éventuel lockdown
   déclenché dans la même requête si seuil atteint.
5. Action spécifique route + ``record_success``.
6. 202 Accepted avec JSON ``CommandResponse``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from polycopy.cli.version import get_version
from polycopy.remote_control.dtos import (
    CommandBody,
    CommandResponse,
    ErrorResponse,
    HealthResponse,
    StatusResponse,
)

if TYPE_CHECKING:
    from polycopy.config import Settings
    from polycopy.remote_control.auth import AutoLockdown, RateLimiter, TOTPGuard
    from polycopy.remote_control.sentinel import SentinelFile

log = structlog.get_logger(__name__)

_RESPAWN_ETA_SECONDS: int = 5


def _peer_ip(request: Request) -> str:
    """Extrait l'IP du peer Tailscale. Fallback 'unknown' si FastAPI n'a pas."""
    if request.client is None:
        return "unknown"
    return request.client.host


def build_app(
    settings: Settings,
    boot_at: datetime,
    *,
    stop_event: asyncio.Event | None = None,
    sentinel: SentinelFile | None = None,
    totp_guard: TOTPGuard | None = None,
    rate_limiter: RateLimiter | None = None,
    lockdown: AutoLockdown | None = None,
) -> FastAPI:
    """Construit l'app FastAPI. POST routes registered ssi toutes deps fournies."""
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
            mode="paused" if sentinel is not None and sentinel.exists() else "running",
            uptime_seconds=uptime_s,
            version=get_version(),
            execution_mode=settings.execution_mode,
            halt_reason=sentinel.reason() if sentinel is not None and sentinel.exists() else None,
        ).model_dump(mode="json")
        return JSONResponse(content=payload)

    # ------------------------------------------------------------------
    # POST routes — Phase C §4.3.3-5
    # ------------------------------------------------------------------

    if (
        stop_event is None
        or sentinel is None
        or totp_guard is None
        or rate_limiter is None
        or lockdown is None
    ):
        return app

    async def _auth_or_response(
        machine: str,
        body: CommandBody,
        request: Request,
    ) -> Response | None:
        """Pipeline auth commune : 404 / 423 / 429 / 401 selon le cas.

        Retourne ``None`` si l'auth est OK (la route appelante peut exécuter
        son action), sinon une ``Response`` prête à retourner.
        """
        if machine.upper() != machine_id_norm:
            return Response(status_code=404)
        if lockdown.is_locked:
            return Response(status_code=423)
        peer = _peer_ip(request)
        if not rate_limiter.allow(peer):
            log.warning("remote_control_rate_limited", peer_ip=peer)
            return JSONResponse(
                status_code=429,
                content=ErrorResponse(error="rate_limited").model_dump(),
            )
        if not totp_guard.verify(body.totp):
            triggered = lockdown.record_failure(peer)
            log.info("remote_control_totp_verify", ok=False, ip=peer)
            if triggered:
                return Response(status_code=423)
            return JSONResponse(
                status_code=401,
                content=ErrorResponse(error="invalid_totp").model_dump(),
            )
        lockdown.record_success(peer)
        log.info("remote_control_totp_verify", ok=True, ip=peer)
        return None

    @app.post("/v1/restart/{machine}")
    async def _restart(machine: str, body: CommandBody, request: Request) -> Response:
        err = await _auth_or_response(machine, body, request)
        if err is not None:
            return err
        log.info(
            "remote_control_command",
            command="restart",
            machine_id=machine_id_norm,
            peer_ip=_peer_ip(request),
            ok=True,
        )
        stop_event.set()
        return JSONResponse(
            status_code=202,
            content=CommandResponse(
                ok=True,
                action="restart",
                respawn_eta_seconds=_RESPAWN_ETA_SECONDS,
                respawn_mode="running",
            ).model_dump(),
        )

    @app.post("/v1/stop/{machine}")
    async def _stop(machine: str, body: CommandBody, request: Request) -> Response:
        err = await _auth_or_response(machine, body, request)
        if err is not None:
            return err
        log.info(
            "remote_control_command",
            command="stop",
            machine_id=machine_id_norm,
            peer_ip=_peer_ip(request),
            ok=True,
        )
        sentinel.touch(reason="manual_stop")
        stop_event.set()
        return JSONResponse(
            status_code=202,
            content=CommandResponse(
                ok=True,
                action="stop",
                respawn_eta_seconds=_RESPAWN_ETA_SECONDS,
                respawn_mode="paused",
            ).model_dump(),
        )

    @app.post("/v1/resume/{machine}")
    async def _resume(machine: str, body: CommandBody, request: Request) -> Response:
        err = await _auth_or_response(machine, body, request)
        if err is not None:
            return err
        if not sentinel.exists():
            log.info(
                "remote_control_command",
                command="resume",
                machine_id=machine_id_norm,
                peer_ip=_peer_ip(request),
                ok=False,
                reason="not_paused",
            )
            return JSONResponse(
                status_code=409,
                content=ErrorResponse(error="not_paused").model_dump(),
            )
        log.info(
            "remote_control_command",
            command="resume",
            machine_id=machine_id_norm,
            peer_ip=_peer_ip(request),
            ok=True,
        )
        sentinel.clear()
        stop_event.set()
        return JSONResponse(
            status_code=202,
            content=CommandResponse(
                ok=True,
                action="resume",
                respawn_eta_seconds=_RESPAWN_ETA_SECONDS,
                respawn_mode="running",
            ).model_dump(),
        )

    return app
