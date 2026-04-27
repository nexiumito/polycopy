"""Client async read-only pour les fees CLOB Polymarket V2.

Endpoint canonique V2 (M18) :

    GET <polymarket_clob_host>/clob-markets/{condition_id}

Réponse 200 : ``{"cid": "...", "mts": 0.01, ..., "fd": {"r": 0.072, "e": 1, "to": true}, ...}``.
Le bloc ``fd`` (FeeDetails) expose :

- ``fd.r`` (float) : ``rate`` parameter de la formule fee (= ``feeRate`` Gamma).
- ``fd.e`` (int)  : ``exponent`` de la formule fee.
- ``fd.to`` (bool) : ``taker_only`` flag (toujours True post-rollout V2).

Marchés fee-free : pas de champ ``fd`` → ``FeeQuote.zero()``.

Formule effective Polymarket V2 :

    effective_rate = fd.r × (p × (1-p))^fd.e

Pré-M18 (V1) — endpoint ``GET /fee-rate?token_id=`` retournait
``{"base_fee": int}`` un FLAG binaire (>0 fee-enabled, =0 fee-free), **pas**
un rate utilisable. M16 dérivait le vrai rate via un mapping hardcodé
``feeType → (rate_param, exponent)`` pour Crypto v2 / Sports v2. M18 supprime
ce mapping car ``fd.e`` + ``fd.r`` sont exposés directement par le protocole
(D6 spec M18 §4.6).

Pattern aligné sur ``ClobReadClient`` (M2) : httpx async + tenacity
exponential backoff + structlog events + cache TTL 60 s + LRU 500.
Single-flight ``dict[str, Future]`` pour dédoublonner les fetches concurrents
sur le même condition_id.

Cf. spec [M18](../../../docs/specs/M18-polymarket-v2-migration.md).
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field
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
"""Worst-case fee rate post-rollout March 30 2026 (cf. spec M16 §11.5).

La doc Polymarket live mentionne explicitement *"Maximum effective fee rate:
1.80%"* pour Sports post-rollout. Crypto v2 plafonne à 1.5625% à p=0.5
(0.25 × 0.25^2). On prend le worst-case pour tous les fallback (réseau down,
HTTP 4xx, payload inattendu) — better over-estimate fee et rejeter un bon
trade que sous-estimer et trader à perte (asymétrie d'impact)."""

_TOKEN_TO_CID_CACHE_MAX = 500
"""Cap LRU du cache `token_id → condition_id` (fallback Gamma D10)."""


class FeeQuote(BaseModel):
    """V2 fee quote — extrait du bloc ``fd`` de ``getClobMarketInfo`` (M18).

    Attributes:
        rate: ``fd.r`` du response, le ``feeRate`` parameter. Range observé
            2026-04-27 : 0.0 (fee-free) ou 0.072 (crypto fee-enabled).
        exponent: ``fd.e`` du response, l'exposant de la formule. Range
            observé : 0 (fee-free) ou 1 (fee-enabled).

    Formule effective : ``effective_rate = rate × (p × (1-p))^exponent``.

    Constantes documentées :
    - ``FeeQuote.zero()`` : marché fee-free (``fd`` absent).
    - ``FeeQuote.conservative_fallback()`` : worst-case 1.80% à p=0.5.
    """

    model_config = ConfigDict(frozen=True)

    rate: Decimal
    exponent: int = Field(ge=0, le=4)

    @classmethod
    def zero(cls) -> FeeQuote:
        """Marché fee-free — ``rate=0, exponent=0``."""
        return cls(rate=Decimal("0"), exponent=0)

    @classmethod
    def conservative_fallback(cls) -> FeeQuote:
        """Fallback réseau down / 5xx — ``rate=0.018, exponent=1``."""
        return cls(rate=_CONSERVATIVE_FALLBACK_RATE, exponent=1)


class FeeRateClient:
    """Client async pour ``GET /clob-markets/{condition_id}`` (V2 — M18).

    Utilisation (path nominal V2) :

        async with httpx.AsyncClient() as http:
            client = FeeRateClient(http, cache_max=500, settings=settings)
            quote = await client.get_fee_quote(asset_id, condition_id=cid)
            # quote.rate, quote.exponent

    Backward-compat M16 :

        rate = await client.get_fee_rate(asset_id)  # deprecated alias

    Attributes:
        ``_inflight``: dict[condition_id → Future] pour single-flight pattern.
        ``_cache``: OrderedDict[condition_id → (FeeQuote, timestamp)] avec TTL
            60s et LRU 500.
        ``_token_to_cid``: OrderedDict[token_id → condition_id] LRU 500 — cache
            de résolution Gamma quand le caller ne fournit pas le ``condition_id``.
        ``_deprecated_warned``: OrderedDict[token_id → True] LRU 500 — warning
            ``get_fee_rate`` 1× par token max.
    """

    DEFAULT_BASE_URL = "https://clob.polymarket.com"
    DEFAULT_GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
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
        self._cache: OrderedDict[str, tuple[FeeQuote, datetime]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future[FeeQuote]] = {}
        self._token_to_cid: OrderedDict[str, str] = OrderedDict()
        self._deprecated_warned: OrderedDict[str, bool] = OrderedDict()
        self._settings = settings
        self._base_url = (
            settings.polymarket_clob_host if settings is not None else self.DEFAULT_BASE_URL
        )

    async def get_fee_quote(
        self,
        token_id: str,
        *,
        condition_id: str | None = None,
    ) -> FeeQuote:
        """Retourne le ``FeeQuote(rate, exponent)`` du marché.

        Path nominal V2 : ``condition_id`` fourni → call direct
        ``GET /clob-markets/{cid}``, zéro Gamma overhead.

        Path fallback V1 / safety net : ``condition_id=None`` → résolution
        ``token_id → condition_id`` via Gamma ``/markets-by-token/{token_id}``
        (cache LRU dédié, max 500). Warning structlog
        ``fee_rate_client_token_id_resolved_via_gamma`` pour signaler
        l'inefficacité.

        Erreurs / fallback :

        - HTTP 404 sur ``/clob-markets/{cid}`` → ``FeeQuote.zero()`` (marché
          inconnu = pas de fee à appliquer).
        - HTTP 400 / 5xx post-tenacity → ``FeeQuote.conservative_fallback()``
          (worst-case 1.80%, cohérent M16 §11.5).
        - ``result["fd"]`` absent → ``FeeQuote.zero()`` (marché fee-free).
        """
        if condition_id is None:
            condition_id = await self._resolve_token_to_condition(token_id)

        now = self._now()
        cached = self._cache.get(condition_id)
        if cached is not None and (now - cached[1]) < _CACHE_TTL:
            self._cache.move_to_end(condition_id)
            log.debug(
                "clob_market_fee_quote_cache_hit",
                condition_id=condition_id,
                rate=str(cached[0].rate),
                exponent=cached[0].exponent,
            )
            return cached[0]

        existing = self._inflight.get(condition_id)
        if existing is not None:
            return await existing

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[FeeQuote] = loop.create_future()
        self._inflight[condition_id] = fut
        try:
            quote = await self._fetch_and_cache(condition_id, now)
            fut.set_result(quote)
            return quote
        except BaseException as exc:
            fut.set_exception(exc)
            raise
        finally:
            self._inflight.pop(condition_id, None)

    async def get_fee_rate(self, token_id: str) -> Decimal:
        """[DEPRECATED M18] Wrapper rétrocompat M16 — retourne ``quote.rate``.

        Émet un warning structlog ``fee_rate_client_get_fee_rate_deprecated``
        une seule fois par ``token_id`` (LRU 500). À retirer en M19+ après
        audit que toutes les callers utilisent ``get_fee_quote``.
        """
        if token_id not in self._deprecated_warned:
            log.warning(
                "fee_rate_client_get_fee_rate_deprecated",
                token_id=token_id,
                reason=(
                    "Utiliser get_fee_quote(token_id, condition_id=...) pour "
                    "accès au quote.exponent. À retirer en M19+."
                ),
            )
            self._deprecated_warned[token_id] = True
            while len(self._deprecated_warned) > _TOKEN_TO_CID_CACHE_MAX:
                self._deprecated_warned.popitem(last=False)
        quote = await self.get_fee_quote(token_id)
        return quote.rate

    async def _resolve_token_to_condition(self, token_id: str) -> str:
        """Fallback Gamma `/markets-by-token/{token_id}` → `condition_id`.

        Cache LRU dédié (`_token_to_cid`, max 500). Émet un warning
        ``fee_rate_client_token_id_resolved_via_gamma`` (chaque résolution non
        cachée — ce path n'est pas le path nominal du pipeline polycopy).
        """
        cached = self._token_to_cid.get(token_id)
        if cached is not None:
            self._token_to_cid.move_to_end(token_id)
            return cached

        log.warning(
            "fee_rate_client_token_id_resolved_via_gamma",
            token_id=token_id,
            reason="condition_id not provided to get_fee_quote",
        )
        gamma_base = self.DEFAULT_GAMMA_BASE_URL
        url = f"{gamma_base}/markets-by-token/{token_id}"
        response = await self._http.get(url, timeout=self.DEFAULT_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise httpx.HTTPStatusError(
                f"unexpected payload type: {type(data).__name__}",
                request=response.request,
                response=response,
            )
        cid_raw = data.get("conditionId") or data.get("condition_id")
        if not isinstance(cid_raw, str) or not cid_raw:
            raise httpx.HTTPStatusError(
                f"markets-by-token missing conditionId for token_id={token_id!r}",
                request=response.request,
                response=response,
            )
        cid = cid_raw
        self._token_to_cid[token_id] = cid
        self._token_to_cid.move_to_end(token_id)
        while len(self._token_to_cid) > _TOKEN_TO_CID_CACHE_MAX:
            self._token_to_cid.popitem(last=False)
        return cid

    async def _fetch_and_cache(self, condition_id: str, now: datetime) -> FeeQuote:
        """Fetch HTTP + parse + cache update + LRU eviction.

        L'ordre est important : on update le cache **avant** ``set_result``
        du Future pour minimiser la race window où une 3ᵉ coroutine pourrait
        lancer un re-fetch redondant.
        """
        try:
            payload = await self._fetch_v2(condition_id)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                log.debug("clob_market_not_found", condition_id=condition_id)
                quote = FeeQuote.zero()
            else:
                log.warning(
                    "clob_market_fetch_failed_using_conservative_fallback",
                    condition_id=condition_id,
                    status=status,
                    error=str(exc)[:128],
                )
                quote = FeeQuote.conservative_fallback()
        except httpx.TransportError as exc:
            log.warning(
                "clob_market_fetch_failed_using_conservative_fallback",
                condition_id=condition_id,
                error=type(exc).__name__,
            )
            quote = FeeQuote.conservative_fallback()
        else:
            quote = self._parse_fee_details(payload)
            log.debug(
                "clob_market_fee_quote_fetched",
                condition_id=condition_id,
                rate=str(quote.rate),
                exponent=quote.exponent,
                taker_only=bool((payload.get("fd") or {}).get("to", True)),
            )

        self._cache[condition_id] = (quote, now)
        self._cache.move_to_end(condition_id)
        while len(self._cache) > self._cache_max:
            evicted_cid, _ = self._cache.popitem(last=False)
            log.debug("clob_market_cache_lru_evicted", condition_id=evicted_cid)
        return quote

    @staticmethod
    def _parse_fee_details(payload: dict[str, Any]) -> FeeQuote:
        """Extrait ``FeeQuote`` depuis ``getClobMarketInfo`` payload V2.

        Branches :
        - ``fd`` absent / None / not-a-dict → ``FeeQuote.zero()`` (fee-free).
        - ``fd.r`` None ou ``""`` → rate=0 (fail-safe).
        - Numeric coerced via ``Decimal(str(...))`` (jamais ``Decimal(float)``).
        """
        fd_raw = payload.get("fd")
        if not isinstance(fd_raw, dict):
            return FeeQuote.zero()
        rate_raw = fd_raw.get("r")
        rate = Decimal("0") if rate_raw is None or rate_raw == "" else Decimal(str(rate_raw))
        exponent_raw = fd_raw.get("e", 0)
        try:
            exponent = int(exponent_raw)
        except (TypeError, ValueError):
            exponent = 0
        if exponent < 0 or exponent > 4:
            # Hors range Pydantic — clamp safe (pas de raise sur payload server).
            exponent = max(0, min(exponent, 4))
        return FeeQuote(rate=rate, exponent=exponent)

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
    async def _fetch_v2(self, condition_id: str) -> dict[str, Any]:
        """``GET /clob-markets/{condition_id}`` — V2 endpoint."""
        url = f"{self._base_url}/clob-markets/{condition_id}"
        response = await self._http.get(url, timeout=self.DEFAULT_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise httpx.HTTPStatusError(
                f"unexpected payload type: {type(data).__name__}",
                request=response.request,
                response=response,
            )
        return data

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=UTC)
