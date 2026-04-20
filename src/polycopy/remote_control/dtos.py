"""DTOs Pydantic v2 du package ``remote_control`` (M12_bis §4.3).

Schémas de réponse des routes HTTP :
- ``HealthResponse`` — ``GET /v1/health``.
- ``StatusResponse`` — ``GET /v1/status/<machine>``.

Phase B : seuls ``health`` + ``status`` existent. Les DTOs des commandes
destructives (``CommandResponse``, ``ErrorResponse``) arrivent Phase C.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class HealthResponse(BaseModel):
    """Liveness probe — Tailscale-only, aucune auth, rien de sensible."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    reason: str | None = None


class StatusResponse(BaseModel):
    """Snapshot lecture-seule de l'état du bot (M12_bis §4.3.2).

    Phase B minimale : ``mode`` toujours ``"running"`` (Phase D ajoute
    ``"paused"``). Les champs optionnels (``heartbeat_index``,
    ``positions_open``, ``pnl_today_usdc``, ``halt_reason``,
    ``halted_since``) restent ``None`` tant que les modules qui les
    populent ne sont pas câblés — le contrat JSON reste stable pour le
    client (iOS Shortcut / curl).
    """

    model_config = ConfigDict(frozen=True)

    machine_id: str
    mode: Literal["running", "paused"]
    uptime_seconds: int
    version: str
    execution_mode: Literal["simulation", "dry_run", "live"]
    heartbeat_index: int | None = None
    positions_open: int | None = None
    pnl_today_usdc: float | None = None
    halt_reason: str | None = None
    halted_since: str | None = None


class CommandBody(BaseModel):
    """Body JSON des routes destructives ``/restart`` ``/stop`` ``/resume``.

    M12_bis §4.3 : TOTP 1-call dans le body (pas de challenge-response
    2-call). Le pattern ``^\\d{6}$`` est vérifié côté ``TOTPGuard`` ; on
    laisse Pydantic accepter tout ``str`` et on rejette proprement via
    HTTP 401.
    """

    model_config = ConfigDict(frozen=True)

    totp: str


class CommandResponse(BaseModel):
    """Réponse 202 Accepted des routes destructives (§4.3.3-5)."""

    model_config = ConfigDict(frozen=True)

    ok: Literal[True]
    action: Literal["restart", "stop", "resume"]
    respawn_eta_seconds: int
    respawn_mode: Literal["running", "paused"]


class ErrorResponse(BaseModel):
    """Réponse 4xx/5xx uniformes (§4.3)."""

    model_config = ConfigDict(frozen=True)

    ok: Literal[False] = False
    error: str
