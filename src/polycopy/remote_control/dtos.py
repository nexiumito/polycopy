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
