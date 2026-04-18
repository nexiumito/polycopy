"""Client async pour la Gamma API Polymarket (`GET /markets`).

M11 : refactor du cache in-memory derrière ``_CacheEntry`` (TTL par entrée)
pour supporter le TTL adaptatif par segment (`_cache_policy.compute_ttl`).
Comportement M2 (TTL 60 s uniforme) préservé via
``settings.strategy_gamma_adaptive_cache_enabled=False``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from polycopy.strategy._cache_policy import compute_ttl
from polycopy.strategy.dtos import MarketMetadata

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)
_tenacity_log = logging.getLogger(__name__)

_HIT_RATE_LOG_INTERVAL_SECONDS: float = 300.0


@dataclass(frozen=True)
class _CacheEntry:
    """Entrée du cache Gamma avec TTL propre (M11)."""

    market: MarketMetadata
    cached_at: datetime
    ttl_seconds: int


class GammaApiClient:
    """Client REST ``https://gamma-api.polymarket.com/markets``.

    Cache in-memory par ``condition_id`` :
    - M2 (default) : TTL uniforme 60 s.
    - M11 (``strategy_gamma_adaptive_cache_enabled=True``) : TTL adaptatif
      selon segment (résolu / proche résolution / actif / inactif), cf.
      ``_cache_policy.compute_ttl``.
    """

    BASE_URL = "https://gamma-api.polymarket.com"
    DEFAULT_TIMEOUT = 10.0
    CACHE_TTL = timedelta(seconds=60)

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        settings: Settings | None = None,
    ) -> None:
        self._http = http_client
        self._settings = settings
        self._cache: dict[str, _CacheEntry] = {}
        self._hits: int = 0
        self._misses: int = 0
        # On utilise `time.monotonic()` plutôt que `self._now()` pour le throttle
        # du log `gamma_cache_hit_rate` : indépendant du monkeypatch `_now` dans
        # les tests M2 (qui inspectent le call count sur les branches cache).
        self._last_log_monotonic: float = time.monotonic()

    async def get_market(self, condition_id: str) -> MarketMetadata | None:
        """Retourne le marché ou ``None`` si Gamma renvoie un array vide.

        Cache policy : miss → HTTP + insert. Hit si ``now - cached_at <
        ttl_seconds``. Compteurs ``_hits`` / ``_misses`` remontent un log
        ``gamma_cache_hit_rate`` toutes les 5 minutes (§4.4 spec M11).

        Note : le pattern `self._now()` est appelé **par branche** pour
        préserver la sémantique M2 (cf. `test_gamma_client.py` qui inspecte
        le nombre d'appels).
        """
        cached = self._cache.get(condition_id)
        if cached is not None and (
            (self._now() - cached.cached_at).total_seconds() < cached.ttl_seconds
        ):
            self._hits += 1
            log.debug("gamma_cache_hit", condition_id=condition_id)
            self._maybe_log_hit_rate()
            return cached.market
        self._misses += 1
        log.debug("gamma_cache_miss", condition_id=condition_id)
        markets = await self._fetch(condition_id)
        if not markets:
            self._maybe_log_hit_rate()
            return None
        market = MarketMetadata.model_validate(markets[0])
        now = self._now()
        self._cache[condition_id] = _CacheEntry(
            market=market,
            cached_at=now,
            ttl_seconds=self._ttl_for(market, now),
        )
        self._maybe_log_hit_rate()
        return market

    async def get_markets_by_condition_ids(
        self,
        condition_ids: list[str],
    ) -> list[MarketMetadata]:
        """Batch fetch ``GET /markets?condition_ids=<csv>`` (M8 resolution).

        Pas de cache (les états ``closed`` changent au fil de l'eau et le
        cycle de polling M8 est de 30 min). Skip silencieusement les marchés
        dont le payload Gamma ne parse pas.
        """
        if not condition_ids:
            return []
        response = await self._http.get(
            f"{self.BASE_URL}/markets",
            params={"condition_ids": ",".join(condition_ids)},
            timeout=self.DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise httpx.HTTPStatusError(
                f"unexpected payload type: {type(data).__name__}",
                request=response.request,
                response=response,
            )
        markets: list[MarketMetadata] = []
        for item in data:
            try:
                markets.append(MarketMetadata.model_validate(item))
            except Exception:  # noqa: BLE001 — skip on schema drift
                log.warning("gamma_resolution_market_parse_skipped")
        return markets

    async def list_top_markets(
        self,
        *,
        limit: int = 20,
        only_active: bool = True,
    ) -> list[MarketMetadata]:
        """Top-N marchés triés par liquidité descendante (usage M5 `/holders` bootstrap).

        Non-caché : appelé ~1 fois par cycle discovery (6h).
        """
        params: dict[str, Any] = {
            "limit": limit,
            "order": "liquidityNum",
            "ascending": "false",
        }
        if only_active:
            params["active"] = "true"
            params["closed"] = "false"
        response = await self._http.get(
            f"{self.BASE_URL}/markets",
            params=params,
            timeout=self.DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise httpx.HTTPStatusError(
                f"unexpected payload type: {type(data).__name__}",
                request=response.request,
                response=response,
            )
        markets: list[MarketMetadata] = []
        for item in data:
            try:
                markets.append(MarketMetadata.model_validate(item))
            except Exception:  # noqa: BLE001 — skip on schema drift
                log.warning("gamma_top_market_parse_skipped")
        return markets

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(_tenacity_log, logging.WARNING),
        reraise=True,
    )
    async def _fetch(self, condition_id: str) -> list[dict[str, Any]]:
        response = await self._http.get(
            f"{self.BASE_URL}/markets",
            params={"condition_ids": condition_id},
            timeout=self.DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise httpx.HTTPStatusError(
                f"unexpected payload type: {type(data).__name__}",
                request=response.request,
                response=response,
            )
        return data

    def _ttl_for(self, market: MarketMetadata, now: datetime) -> int:
        """Compute TTL en respectant le feature flag M11.

        - Flag ON (default) → ``compute_ttl`` adaptatif.
        - Flag OFF → TTL 60 s uniforme M2 (``CACHE_TTL.total_seconds()``).
        """
        if self._settings is not None and self._settings.strategy_gamma_adaptive_cache_enabled:
            return compute_ttl(market, now)
        return int(self.CACHE_TTL.total_seconds())

    def _maybe_log_hit_rate(self) -> None:
        """Émet ``gamma_cache_hit_rate`` toutes les 5 min puis reset les compteurs.

        Utilise ``time.monotonic()`` (indépendant de ``_now()`` pour les tests).
        """
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_log_monotonic < _HIT_RATE_LOG_INTERVAL_SECONDS:
            return
        total = self._hits + self._misses
        ratio = (self._hits / total) if total else 0.0
        log.info(
            "gamma_cache_hit_rate",
            hit_rate=round(ratio, 4),
            hits=self._hits,
            misses=self._misses,
            window_seconds=int(_HIT_RATE_LOG_INTERVAL_SECONDS),
        )
        self._last_log_monotonic = now_monotonic
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=UTC)
