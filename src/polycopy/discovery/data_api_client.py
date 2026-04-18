"""Client async pour les endpoints Data API publics consommés par M5.

Ce client étend `watcher/data_api_client.py` avec les endpoints M5 spécifiques
(`/holders`, `/trades` feed global, `/value`, `/positions` et `/activity` non
filtrés par type). Toutes les requêtes passent par un `asyncio.Semaphore` pour
plafonner la concurrence à 5 et rester sous ~60 req/min observé en pic (cf.
spec §0.5 + §8.3).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import httpx
import structlog
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from polycopy.discovery.dtos import (
    GlobalTrade,
    HolderEntry,
    RawPosition,
    WalletValue,
)

log = structlog.get_logger(__name__)
_tenacity_log = logging.getLogger(__name__)


class DiscoveryDataApiClient:
    """Client REST pour les endpoints discovery de la Data API Polymarket."""

    BASE_URL = "https://data-api.polymarket.com"
    DEFAULT_TIMEOUT = 15.0
    MAX_CONCURRENT = 5  # cf. spec §8.3 throttle

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        self._http = http_client
        self._sem = semaphore or asyncio.Semaphore(self.MAX_CONCURRENT)

    # --- /holders ---------------------------------------------------------

    async def get_holders(self, market: str, *, limit: int = 20) -> list[HolderEntry]:
        """Retourne top holders d'un marché (dédup par `proxyWallet`).

        L'API renvoie un array de ``HolderGroup`` (1 par token) ; pour un
        marché binaire YES/NO il y a 2 groupes. On collapse et dédup côté client.
        """
        raw = await self._fetch(
            "/holders",
            params={"market": market, "limit": limit},
        )
        if not isinstance(raw, list):
            return []
        seen: set[str] = set()
        holders: list[HolderEntry] = []
        for group in raw:
            if not isinstance(group, dict):
                continue
            for holder_raw in group.get("holders", []) or []:
                wallet = (holder_raw.get("proxyWallet") or "").lower()
                if not wallet or wallet in seen:
                    continue
                seen.add(wallet)
                try:
                    holders.append(HolderEntry.model_validate(holder_raw))
                except Exception:  # noqa: BLE001 — tolère schéma bonus
                    log.warning("holders_parse_skipped", wallet=wallet)
        return holders

    # --- /trades (feed global) --------------------------------------------

    async def get_global_trades(
        self,
        *,
        limit: int = 500,
        min_usdc_size: float = 100.0,
        taker_only: bool = True,
    ) -> list[GlobalTrade]:
        """Feed global des N derniers trades, filtré côté serveur par taille USD.

        ⚠️ Divergence §14.5 #2 confirmée : le champ `usdcSize` n'est PAS renvoyé
        par l'API. Le filtre `filterAmount` est appliqué server-side (trades
        observés > min_usdc_size) mais on doit recalculer ``size × price``
        pour les cas où on veut filtrer plus fin côté client.
        """
        params: dict[str, Any] = {
            "limit": limit,
            "takerOnly": str(taker_only).lower(),
            "filterType": "CASH",
            "filterAmount": int(min_usdc_size),
        }
        raw = await self._fetch("/trades", params=params)
        if not isinstance(raw, list):
            return []
        trades: list[GlobalTrade] = []
        for item in raw:
            try:
                trades.append(GlobalTrade.model_validate(item))
            except Exception:
                log.warning("global_trades_parse_skipped")
        return trades

    # --- /value -----------------------------------------------------------

    async def get_value(self, user: str) -> float:
        """USD value totale des positions ouvertes d'un wallet.

        Retourne 0.0 si l'endpoint renvoie vide (wallet jamais vu ou cashed-out).
        """
        raw = await self._fetch("/value", params={"user": user.lower()})
        if not isinstance(raw, list) or not raw:
            return 0.0
        try:
            entry = WalletValue.model_validate(raw[0])
            return float(entry.value)
        except Exception:
            return 0.0

    # --- /positions -------------------------------------------------------

    async def get_positions(
        self,
        user: str,
        *,
        limit: int = 500,
        sort_direction: str = "DESC",
    ) -> list[RawPosition]:
        """Positions d'un wallet (ouvertes + résolues, triées par CASHPNL).

        Jusqu'à 500 positions par page, pas de pagination M5 (tronqué au `limit`).
        """
        raw = await self._fetch(
            "/positions",
            params={
                "user": user.lower(),
                "limit": limit,
                "sortBy": "CASHPNL",
                "sortDirection": sort_direction,
            },
        )
        if not isinstance(raw, list):
            return []
        positions: list[RawPosition] = []
        for item in raw:
            try:
                positions.append(RawPosition.model_validate(item))
            except Exception:
                log.warning("positions_parse_skipped", user=user)
        return positions

    # --- /activity (non filtré sur type) ----------------------------------

    async def get_activity_trades(
        self,
        user: str,
        *,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Retourne les trades (`type=TRADE`) du wallet depuis `since`.

        Contrairement au `watcher/DataApiClient.get_trades` qui valide en DTO
        `TradeActivity`, on renvoie ici des dicts bruts — le metrics_collector
        n'a besoin que de `conditionId` + `size` × `price` pour calculer volume
        et Herfindahl, inutile de payer le coût d'un DTO validation.
        """
        params: dict[str, Any] = {
            "user": user.lower(),
            "type": "TRADE",
            "limit": limit,
            "sortDirection": "DESC",
        }
        if since is not None:
            params["start"] = int(since.timestamp())
        raw = await self._fetch("/activity", params=params)
        if not isinstance(raw, list):
            return []
        return [r for r in raw if isinstance(r, dict) and r.get("type") == "TRADE"]

    # --- plomberie HTTP ---------------------------------------------------

    async def _fetch(
        self,
        path: str,
        *,
        params: dict[str, Any],
    ) -> Any:
        async with self._sem:
            return await self._fetch_with_retry(path, params=params)

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(4),
        before_sleep=before_sleep_log(_tenacity_log, logging.WARNING),
        reraise=True,
    )
    async def _fetch_with_retry(
        self,
        path: str,
        *,
        params: dict[str, Any],
    ) -> Any:
        response = await self._http.get(
            f"{self.BASE_URL}{path}",
            params=params,
            timeout=self.DEFAULT_TIMEOUT,
        )
        if response.status_code == 404:
            return []  # /holders sur marché inconnu → silence
        response.raise_for_status()
        return response.json()
