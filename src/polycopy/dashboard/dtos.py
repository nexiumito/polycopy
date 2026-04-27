"""DTOs Pydantic v2 du dashboard M6 (cosmétique uniquement, pas de logique métier)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class KpiCard(BaseModel):
    """KPI card rendue par la Home (4 cards, layout grid).

    Tous les champs textuels sont **déjà formatés** côté query (cf. spec §6.1) —
    Jinja se contente d'afficher. M19 MH.7 : ``value`` reste pré-formaté
    (rétrocompat tests M6) mais utilise désormais ``format_usd`` côté query
    pour cohérence /home ↔ /performance ; ``value_raw`` expose la valeur
    numérique pour audit. M19 MH.4 : ``tooltip`` optionnel rend une icône
    info à côté du titre.
    """

    model_config = ConfigDict(frozen=True)

    title: str
    value: str
    value_raw: float | None = None
    delta: str | None
    delta_sign: Literal["positive", "negative", "neutral"] | None
    sparkline_points: list[tuple[datetime, float]]
    icon: str
    tooltip: str | None = None


class DiscoveryStatus(BaseModel):
    """Fragment 'Discovery status' Home (M5)."""

    model_config = ConfigDict(frozen=True)

    enabled: bool
    active_count: int
    shadow_count: int
    paused_count: int
    pinned_count: int
    last_cycle_at: datetime | None
    promotions_24h: int
    demotions_24h: int


class PnlMilestone(BaseModel):
    """Marqueur temporel dans la timeline PnL (sous le graphique Chart.js)."""

    model_config = ConfigDict(frozen=True)

    at: datetime
    event_type: Literal[
        "first_trade",
        "first_fill",
        "kill_switch",
        "trader_promoted",
        "cycle_completed",
    ]
    label: str
    wallet_address: str | None
    market_slug: str | None


__all__ = ["DiscoveryStatus", "KpiCard", "PnlMilestone"]
