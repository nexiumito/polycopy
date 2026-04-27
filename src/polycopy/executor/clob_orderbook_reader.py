"""Reader CLOB ``GET /book`` (M8) — read-only public, cache TTL 5s + LRU.

Aucune auth, aucune signature. Cohérent avec le triple garde-fou M3 :
M8 ne touche jamais à ``ClobWriteClient`` ni aux creds L1/L2.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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

from polycopy.executor.dtos import Orderbook, OrderbookLevel

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)
_tenacity_log = logging.getLogger(__name__)


class OrderbookNotFoundError(Exception):
    """Le ``/book?token_id=`` a renvoyé 404 — token inconnu ou retiré."""


class ClobOrderbookReader:
    """Wrapper httpx pour ``GET <polymarket_clob_host>/book?token_id=...``.

    Cache in-memory par ``asset_id`` avec TTL configurable (5 s default) et
    LRU cap (500 entries default) pour éviter les fuites mémoire sur runs
    longs. Pic théorique post-cache : < 5 req/min observé.

    M18 : `BASE_URL` consommé via `settings.polymarket_clob_host` (D7).
    `settings=None` reste accepté pour rétrocompat tests M2..M17 (default
    `https://clob.polymarket.com`).
    """

    DEFAULT_BASE_URL = "https://clob.polymarket.com"
    DEFAULT_TIMEOUT = 5.0

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        ttl_seconds: int = 5,
        max_entries: int = 500,
        settings: Settings | None = None,
    ) -> None:
        self._http = http_client
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max = max_entries
        self._store: OrderedDict[str, tuple[datetime, Orderbook]] = OrderedDict()
        self._base_url = (
            settings.polymarket_clob_host if settings is not None else self.DEFAULT_BASE_URL
        )

    async def get_orderbook(self, asset_id: str) -> Orderbook:
        """Retourne le book courant pour ``asset_id``. Cache TTL appliqué."""
        now = self._now()
        entry = self._store.get(asset_id)
        if entry is not None:
            cached_at, book = entry
            if now - cached_at < self._ttl:
                # LRU : remet en queue (most-recent).
                self._store.move_to_end(asset_id)
                log.debug("clob_orderbook_cache_hit", asset_id=asset_id)
                return book
        log.debug("clob_orderbook_cache_miss", asset_id=asset_id)
        book = await self._fetch(asset_id)
        self._store[asset_id] = (now, book)
        self._store.move_to_end(asset_id)
        if len(self._store) > self._max:
            # Evict the least-recently used entry.
            self._store.popitem(last=False)
        return book

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        stop=stop_after_attempt(3),
        before_sleep=before_sleep_log(_tenacity_log, logging.WARNING),
        reraise=True,
    )
    async def _fetch(self, asset_id: str) -> Orderbook:
        try:
            response = await self._http.get(
                f"{self._base_url}/book",
                params={"token_id": asset_id},
                timeout=self.DEFAULT_TIMEOUT,
            )
        except httpx.HTTPStatusError as exc:  # pragma: no cover — wrapper level
            raise exc
        if response.status_code == 404:
            log.warning("clob_orderbook_not_found", asset_id=asset_id)
            raise OrderbookNotFoundError(asset_id)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise httpx.HTTPStatusError(
                f"unexpected payload type: {type(data).__name__}",
                request=response.request,
                response=response,
            )
        return _parse_orderbook(data, asset_id, self._now())

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=UTC)


def _parse_orderbook(payload: dict[str, Any], asset_id: str, snapshot_at: datetime) -> Orderbook:
    """Parse une réponse ``/book`` en ``Orderbook`` Pydantic.

    ``price`` et ``size`` sont des strings (precision arbitraire) — convertir
    via ``Decimal(str(...))`` (jamais ``Decimal(float)``).
    """
    bids = sorted(
        (_parse_level(b) for b in payload.get("bids", [])),
        key=lambda lvl: -lvl.price,
    )
    asks = sorted(
        (_parse_level(a) for a in payload.get("asks", [])),
        key=lambda lvl: lvl.price,
    )
    raw_hash = payload.get("hash")
    return Orderbook(
        asset_id=str(payload.get("asset_id", asset_id)),
        bids=bids,
        asks=asks,
        snapshot_at=snapshot_at,
        raw_hash=str(raw_hash) if raw_hash is not None else None,
    )


def _parse_level(raw: dict[str, Any]) -> OrderbookLevel:
    price_raw = raw["price"]
    size_raw = raw["size"]
    return OrderbookLevel(
        price=Decimal(str(price_raw)),
        size=Decimal(str(size_raw)),
    )
