"""Politique de TTL adaptatif du cache Gamma (M11 §4).

Fonction pure ``compute_ttl(market, now) -> int`` : aucun I/O, aucun state.
Le résultat dépend **uniquement** de la paire ``(market, now)`` — testable
isolément et safe pour du reasoning par segment (cf. spec M11 §4.1 tableau).

Segments :

- Résolu (``closed=True`` OR ``archived=True``) → ``_TTL_RESOLVED_SENTINEL``
  (immuable effectif).
- Proche résolution (``end_date - now < 1h`` ET non résolu) → 10 s
  (volatilité liquidité/état max sur la dernière heure).
- Actif (``volume_24h_usd > 100`` OR ``liquidity_clob > 1000``) → 300 s
  (vs 60 s uniforme M2, champs consommés par ``MarketFilter`` ne bougent
  pas à la seconde).
- Inactif (par défaut) → 3600 s (peu d'entrées dans ``detected_trades``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polycopy.strategy.dtos import MarketMetadata

_TTL_RESOLVED_SENTINEL: int = 31_536_000
_TTL_NEAR_RESOLUTION_SECONDS: int = 10
_TTL_ACTIVE_SECONDS: int = 300
_TTL_INACTIVE_SECONDS: int = 3600

_NEAR_RESOLUTION_WINDOW: timedelta = timedelta(hours=1)
_ACTIVE_VOLUME_24H_USD: float = 100.0
_ACTIVE_LIQUIDITY_USD: float = 1000.0


def compute_ttl(market: MarketMetadata, now: datetime) -> int:
    """Retourne le TTL cache en secondes pour un marché donné.

    Pure function. Voir docstring module pour la sémantique des 4 segments.
    """
    if market.closed or market.archived:
        return _TTL_RESOLVED_SENTINEL
    end = _resolve_end_datetime(market)
    if end is not None and (end - now) < _NEAR_RESOLUTION_WINDOW:
        return _TTL_NEAR_RESOLUTION_SECONDS
    volume = _extract_volume_24h(market)
    if volume > _ACTIVE_VOLUME_24H_USD:
        return _TTL_ACTIVE_SECONDS
    if (market.liquidity_clob or 0.0) > _ACTIVE_LIQUIDITY_USD:
        return _TTL_ACTIVE_SECONDS
    return _TTL_INACTIVE_SECONDS


def _resolve_end_datetime(market: MarketMetadata) -> datetime | None:
    """Helper duplicated from ``pipeline.py:56-72`` (rule of three non atteinte).

    TODO M12 : promouvoir en méthode ``MarketMetadata.resolve_end_datetime``
    (cf. risque §11.5 spec M11).
    """
    end_date = market.end_date
    if end_date is not None:
        return end_date if end_date.tzinfo else end_date.replace(tzinfo=UTC)
    end_date_iso = market.end_date_iso
    if end_date_iso is None:
        return None
    try:
        if len(end_date_iso) == 10:
            return datetime.fromisoformat(end_date_iso + "T23:59:59+00:00")
        parsed = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _extract_volume_24h(market: MarketMetadata) -> float:
    """``volume_24h_usd`` n'est pas un champ typé sur ``MarketMetadata``.

    Gamma renvoie ``volume24hr`` (string ou float) et le DTO M2 absorbe via
    ``extra="allow"`` (cf. `strategy/dtos.py:20`). On lit l'attribut dynamique
    avec garde-fou parse. Retourne ``0.0`` si absent ou invalide.
    """
    raw = getattr(market, "volume24hr", None)
    if raw is None:
        raw = getattr(market, "volume_24h_usd", None)
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0
