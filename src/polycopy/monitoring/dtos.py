"""DTOs du module Monitoring (M4 + M7)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

AlertLevel = Literal["INFO", "WARNING", "ERROR", "CRITICAL"]
DigestAction = Literal["emit_single", "emit_digest"]


class Alert(BaseModel):
    """Événement critique à pousser sur la queue alertes (M4).

    ``cooldown_key`` permet au ``AlertDispatcher`` de dédupliquer les alertes
    répétées dans une fenêtre (default 60 s). ``None`` = jamais de throttle.
    """

    model_config = ConfigDict(frozen=True)

    level: AlertLevel
    event: str
    body: str
    cooldown_key: str | None = None


class DigestDecision(BaseModel):
    """Décision du ``AlertDigestWindow`` pour une alerte (M7 §2.5)."""

    model_config = ConfigDict(frozen=True)

    action: DigestAction
    count: int
    event_type: str


class PinnedWallet(BaseModel):
    """Wallet pinned à afficher dans le startup message (M7 §4.2)."""

    model_config = ConfigDict(frozen=True)

    wallet_short: str
    label: str | None = None


class ModuleStatus(BaseModel):
    """Statut d'un module fonctionnel listé dans le startup message."""

    model_config = ConfigDict(frozen=True)

    name: str
    enabled: bool
    detail: str


class StartupContext(BaseModel):
    """Context consommé par ``startup.md.j2`` (M7 §4.2, étendu M10 §3.4, M12_bis §4.2.1).

    ``mode`` reflète la nouvelle enum M10 ``execution_mode`` ; la
    représentation visuelle (badge emoji) est déléguée au filter
    ``mode_badge`` injecté par ``AlertRenderer``.

    M12_bis : ``paused: bool`` — true quand le runner a bifurqué en
    mode paused au boot (sentinel ``halt.flag`` détecté). Le template
    startup ajoute un bloc `{% if paused %}` explicite pour signaler
    l'état et indiquer la commande ``/resume``.
    """

    model_config = ConfigDict(frozen=True)

    version: str
    mode: Literal["simulation", "dry_run", "live"]
    boot_at: datetime
    pinned_wallets: list[PinnedWallet]
    modules: list[ModuleStatus]
    dashboard_url: str | None = None
    paused: bool = False


class ShutdownContext(BaseModel):
    """Context consommé par ``shutdown.md.j2`` (M7 §2.7)."""

    model_config = ConfigDict(frozen=True)

    duration_human: str
    version: str


class HeartbeatContext(BaseModel):
    """Context consommé par ``heartbeat.md.j2`` (M7 §4.3, étendu M12_bis §4.2.1).

    M12_bis : ``paused: bool`` — true quand le process tourne en mode
    paused. Le template heartbeat ajoute un bloc `{% if paused %}`
    pour rappeler l'état paused et la commande ``/resume``.
    """

    model_config = ConfigDict(frozen=True)

    uptime_human: str
    heartbeat_index: int
    watcher_count: int
    positions_open: int
    critical_alerts_in_window: int
    paused: bool = False


class TopWalletEntry(BaseModel):
    """Top wallet actif dans le daily summary."""

    model_config = ConfigDict(frozen=True)

    wallet_short: str
    label: str | None
    trade_count: int


class DailySummaryContext(BaseModel):
    """Context consommé par ``daily_summary.md.j2`` (M7 §4.4)."""

    model_config = ConfigDict(frozen=True)

    date_human: str
    trades_24h: int
    top_wallets: list[TopWalletEntry]
    decisions_approved: int
    decisions_rejected: int
    top_reject_reason: str | None
    orders_sent: int
    orders_filled: int
    orders_rejected: int
    volume_executed_usd: float
    total_usdc: float | None
    delta_24h_pct: float | None
    drawdown_24h_pct: float | None
    positions_open: int
    positions_value_usd: float
    discovery_enabled: bool
    discovery_cycles_24h: int
    discovery_promotions_24h: int
    discovery_demotions_24h: int
    discovery_cap_reached_24h: int
    alerts_total_24h: int
    alerts_by_type_compact: str
    dashboard_url: str | None


class DigestContext(BaseModel):
    """Context consommé par ``digest.md.j2`` (M7 §4.5)."""

    model_config = ConfigDict(frozen=True)

    event_type: str
    count: int
    window_minutes: int
    level: AlertLevel
    sample_lines: list[str]
    truncated_count: int
    dashboard_url: str | None = None
