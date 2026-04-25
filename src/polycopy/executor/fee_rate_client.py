"""Client async read-only pour l'endpoint Polymarket ``/fee-rate``.

Endpoint public no-auth :

    GET https://clob.polymarket.com/fee-rate?token_id=<asset_id>

Réponse 200 : ``{"base_fee": <int>}`` où ``base_fee`` est exprimé en
**basis points** (entier, 1 bp = 0.01 %). Marchés fee-free (vaste majorité
Polymarket pré-March 30 2026 hors Crypto + Sports v2) renvoient
``{"base_fee": 0}``.

Note importante (cf. spec M16 §11.5) : ``base_fee`` est un **flag binaire**
(>0 = fee-enabled, =0 = fee-free), **pas** un rate utilisable directement.
Live API confirmé 2026-04-25 : crypto_fees_v2 et sports_fees_v2 renvoient
tous deux ``{"base_fee": 1000}`` malgré des paramètres formulaire différents
(crypto: feeRate=0.25, exp=2 ; sports: feeRate=0.03, exp=1). Le calcul de
l'effective fee se fait via la formule
``feeRate × (p × (1-p))^exponent`` paramétrée par ``market.fee_type`` Gamma
côté ``PositionSizer._compute_effective_fee_rate``. Le ``FeeRateClient``
expose juste le ``base_fee`` comme Decimal ∈ [0, 1] (= bps / 10000) et le
caller décide quoi en faire.

Pattern aligné sur ``ClobReadClient`` (M2) : httpx async + tenacity
exponential backoff + structlog events + cache TTL 60 s + LRU 500.
Single-flight ``dict[str, Future]`` pour dédoublonner les fetches concurrents
sur le même token_id (TOCTOU fix préventif — audit M-007).

Cf. spec [docs/specs/M16-dynamic-fees-ev.md](../../../docs/specs/M16-dynamic-fees-ev.md).
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import structlog
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)
_tenacity_log = logging.getLogger(__name__)

_CACHE_TTL = timedelta(seconds=60)
"""TTL cohérent avec le cache Gamma M2. Les fees Polymarket ne bougent pas
par seconde — 60 s couvre les changements pratiques sans spam."""

_CONSERVATIVE_FALLBACK_RATE = Decimal("0.018")
"""Worst-case fee rate post-rollout March 30 2026.

Rationale (cf. spec M16 §11.5) : la doc Polymarket live (2026-04-25)
mentionne explicitement *"Maximum effective fee rate: 1.80%"* pour Sports
post-rollout. Crypto v2 plafonne à ~1.56 % (skill cache). On prend le
worst-case pour tous les fallback (réseau down, HTTP 4xx, payload
inattendu) — better over-estimate fee et rejeter un bon trade que
sous-estimer et trader à perte (asymétrie d'impact, décision **D3**)."""


class FeeRateClient:
    """Client async pour ``GET /fee-rate?token_id=...``.

    Utilisation :

        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http, cache_max=500, settings=settings)
            rate = await client.get_fee_rate("3417...")  # Decimal("0.10")

    Attribute Notes :
        ``_inflight``: dict[token_id → Future] pour single-flight pattern.
        Une coroutine A qui call ``get_fee_rate(X)`` crée un Future, B/C
        qui call simultanément ``get_fee_rate(X)`` await le Future de A
        plutôt que de re-fetch — économise N-1 round-trips HTTP.
    """

    BASE_URL = "https://clob.polymarket.com"
    DEFAULT_TIMEOUT = 5.0  # cohérent ClobReadClient (read temps réel)

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        cache_max: int = 500,
        settings: Settings | None = None,
    ) -> None:
        self._http = http_client
        self._cache_max = cache_max
        self._cache: OrderedDict[str, tuple[Decimal, datetime]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[Decimal]] = {}
        self._settings = settings  # réservé pour LATENCY_INSTRUMENTATION ext.

    async def get_fee_rate(self, token_id: str) -> Decimal:
        """Retourne le ``base_fee`` du marché en Decimal ∈ [0, 1] (= bps / 10000).

        Path :

        1. Cache hit (TTL 60 s) → retour immédiat (et touch LRU).
        2. Single-flight inflight → await le Future en cours pour ce token_id.
        3. Sinon : fetch HTTP, parse, cache, retour.

        Erreurs :

        - HTTP 404 / "fee rate not found" → ``Decimal("0")`` (marché fee-free).
        - HTTP 400 / "Invalid token id" → fallback conservateur 1.80 %.
        - 5xx ou ``TransportError`` post-tenacity → fallback conservateur.
        """
        now = datetime.now(tz=UTC)
        cached = self._cache.get(token_id)
        if cached is not None and (now - cached[1]) < _CACHE_TTL:
            self._cache.move_to_end(token_id)
            log.debug("fee_rate_cache_hit", token_id=token_id, rate=str(cached[0]))
            return cached[0]

        existing = self._inflight.get(token_id)
        if existing is not None:
            return await existing

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Decimal] = loop.create_future()
        self._inflight[token_id] = fut
        try:
            rate = await self._fetch_and_cache(token_id, now)
            fut.set_result(rate)
            return rate
        except BaseException as exc:  # noqa: BLE001
            fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(token_id, None)

    async def _fetch_and_cache(self, token_id: str, now: datetime) -> Decimal:
        """Fetch HTTP + parse + cache update + LRU eviction.

        L'ordre est important : on update le cache **avant** ``set_result``
        du Future (cf. §11.8 spec) pour minimiser la race window où une 3ᵉ
        coroutine pourrait lancer un re-fetch redondant.
        """
        try:
            payload = await self._fetch(token_id)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                log.debug("fee_rate_market_not_found", token_id=token_id)
                rate = Decimal("0")
            elif status == 400:
                log.warning(
                    "fee_rate_invalid_token_id",
                    token_id=token_id,
                    body=exc.response.text[:128],
                )
                rate = _CONSERVATIVE_FALLBACK_RATE
            else:
                log.warning(
                    "fee_rate_fetch_failed_using_conservative_fallback",
                    token_id=token_id,
                    status=status,
                    error=str(exc)[:128],
                )
                rate = _CONSERVATIVE_FALLBACK_RATE
        except httpx.TransportError as exc:
            log.warning(
                "fee_rate_fetch_failed_using_conservative_fallback",
                token_id=token_id,
                error=type(exc).__name__,
            )
            rate = _CONSERVATIVE_FALLBACK_RATE
        else:
            base_fee_bps = int(payload.get("base_fee", 0))
            rate = Decimal(base_fee_bps) / Decimal(10_000)
            log.debug(
                "fee_rate_fetched",
                token_id=token_id,
                base_fee_bps=base_fee_bps,
                rate=str(rate),
            )

        self._cache[token_id] = (rate, now)
        self._cache.move_to_end(token_id)
        # LRU eviction inline.
        while len(self._cache) > self._cache_max:
            evicted_token, _ = self._cache.popitem(last=False)
            log.debug("fee_rate_cache_lru_evicted", token_id=evicted_token)
        return rate

    @retry(
        retry=retry_if_exception(
            lambda exc: (
                isinstance(exc, httpx.TransportError)
                or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and (exc.response.status_code == 429 or exc.response.status_code >= 500)
                )
            )
        ),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(_tenacity_log, logging.WARNING),
        reraise=True,
    )
    async def _fetch(self, token_id: str) -> dict[str, int]:
        response = await self._http.get(
            f"{self.BASE_URL}/fee-rate",
            params={"token_id": token_id},
            timeout=self.DEFAULT_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise httpx.HTTPStatusError(
                f"unexpected payload type: {type(data).__name__}",
                request=response.request,
                response=response,
            )
        return data
