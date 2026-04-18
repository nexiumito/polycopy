"""Construction du pool de wallets candidats pour un cycle discovery M5.

Pipeline :
1. Top-K marchés Gamma → /holders fan-out.
2. Feed global /trades filtré par min_usdc_size.
3. Goldsky top-N par realizedPnl (si backend `goldsky` ou `hybrid`).
4. Dédup case-insensitive + exclusion blacklist + tronquage au `pool_size` final.

Règle stratégique (spec §2.1) : pré-filtrer les candidats selon un signal
simple (fréquence × log(volume)) avant d'appeler `/positions` pour chacun —
économie de budget API.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import TYPE_CHECKING

import structlog

from polycopy.discovery.data_api_client import DiscoveryDataApiClient
from polycopy.discovery.dtos import CandidateWallet
from polycopy.discovery.goldsky_client import GoldskyClient

if TYPE_CHECKING:
    from polycopy.config import Settings
    from polycopy.strategy.gamma_client import GammaApiClient

log = structlog.get_logger(__name__)


class CandidatePool:
    """Orchestre la construction du pool candidate avant scoring."""

    def __init__(
        self,
        data_api: DiscoveryDataApiClient,
        gamma: GammaApiClient,
        goldsky: GoldskyClient | None,
        settings: Settings,
    ) -> None:
        self._data_api = data_api
        self._gamma = gamma
        self._goldsky = goldsky
        self._settings = settings

    async def build(
        self,
        *,
        exclude_wallets: set[str] | None = None,
    ) -> list[CandidateWallet]:
        """Retourne les N meilleurs candidats, dédupés + blacklist-filtrés.

        ``exclude_wallets`` sert au caller pour skip les traders déjà en DB avec
        status='paused' (volontairement retirés).
        """
        signals: dict[str, _CandidateSignal] = {}

        # 1. Bootstrap par top markets (/holders fan-out)
        await self._seed_from_holders(signals)

        # 2. Bootstrap par feed global
        await self._seed_from_global_trades(signals)

        # 3. Bootstrap par Goldsky (opt-in)
        if self._goldsky is not None:
            await self._seed_from_goldsky(signals)

        # 4. Filtres : blacklist (normalisation lowercase) + exclusions caller.
        blacklist = {w.lower() for w in self._settings.blacklisted_wallets}
        excludes = {w.lower() for w in (exclude_wallets or set())}
        filtered = {w: s for w, s in signals.items() if w not in blacklist and w not in excludes}

        # 5. Tri par initial_signal DESC + cap `pool_size`.
        ranked = sorted(
            filtered.values(),
            key=lambda s: s.initial_signal,
            reverse=True,
        )
        cap = self._settings.discovery_candidate_pool_size
        ranked = ranked[:cap]

        log.info(
            "discovery_candidates_built",
            total_distinct=len(signals),
            post_filter=len(filtered),
            returned=len(ranked),
            blacklist_hits=len(signals) - len(filtered),
        )
        return [s.to_candidate() for s in ranked]

    async def _seed_from_holders(
        self,
        signals: dict[str, _CandidateSignal],
    ) -> None:
        top_markets = await self._gamma.list_top_markets(
            limit=self._settings.discovery_top_markets_for_holders,
        )
        for market in top_markets:
            holders = await self._data_api.get_holders(market.condition_id, limit=20)
            for h in holders:
                w = h.proxy_wallet.lower()
                s = signals.setdefault(
                    w,
                    _CandidateSignal(
                        wallet_address=w,
                        sources=set(),
                        sample_market=market.condition_id,
                        max_amount=0.0,
                        appearances=0,
                    ),
                )
                s.sources.add("holders")
                s.appearances += 1
                s.max_amount = max(s.max_amount, float(h.amount))

    async def _seed_from_global_trades(
        self,
        signals: dict[str, _CandidateSignal],
    ) -> None:
        min_usd = 100.0  # filtrage server-side (paramètre `filterAmount` API).
        trades = await self._data_api.get_global_trades(
            limit=500,
            min_usdc_size=min_usd,
        )
        # On ré-agrège volume par wallet (usdc_size = size × price, §14.5 #2).
        wallet_volume: dict[str, float] = defaultdict(float)
        wallet_sample: dict[str, str] = {}
        for t in trades:
            w = t.proxy_wallet.lower()
            v = t.usdc_size
            if v < min_usd:
                continue  # defense in depth si le filtre server-side rate
            wallet_volume[w] += v
            wallet_sample.setdefault(w, t.condition_id)
        for w, vol in wallet_volume.items():
            s = signals.setdefault(
                w,
                _CandidateSignal(
                    wallet_address=w,
                    sources=set(),
                    sample_market=wallet_sample.get(w),
                    max_amount=0.0,
                    appearances=0,
                ),
            )
            s.sources.add("global_trades")
            s.max_amount = max(s.max_amount, vol)
            s.appearances += 1

    async def _seed_from_goldsky(
        self,
        signals: dict[str, _CandidateSignal],
    ) -> None:
        if self._goldsky is None:
            return
        try:
            positions = await self._goldsky.top_wallets_by_realized_pnl(first=200)
        except Exception:
            log.exception("goldsky_seed_failed")
            return
        for p in positions:
            w = p.user.lower()
            s = signals.setdefault(
                w,
                _CandidateSignal(
                    wallet_address=w,
                    sources=set(),
                    sample_market=None,
                    max_amount=0.0,
                    appearances=0,
                ),
            )
            s.sources.add("goldsky")
            s.appearances += 1
            try:
                pnl = float(p.realized_pnl) / 1_000_000.0  # scale 10⁶ USDC empirique
            except (TypeError, ValueError):
                pnl = 0.0
            s.max_amount = max(s.max_amount, pnl)


class _CandidateSignal:
    """État intermédiaire par wallet pendant la construction du pool."""

    __slots__ = ("appearances", "max_amount", "sample_market", "sources", "wallet_address")

    def __init__(
        self,
        *,
        wallet_address: str,
        sources: set[str],
        sample_market: str | None,
        max_amount: float,
        appearances: int,
    ) -> None:
        self.wallet_address = wallet_address
        self.sources = sources
        self.sample_market = sample_market
        self.max_amount = max_amount
        self.appearances = appearances

    @property
    def initial_signal(self) -> float:
        """Signal pré-scoring = appearances + log10(max(1, max_amount))."""
        return float(self.appearances) + math.log10(max(1.0, float(self.max_amount)))

    def to_candidate(self) -> CandidateWallet:
        # Priorité des sources pour le tag : holders > global_trades > goldsky.
        if "holders" in self.sources:
            via = "holders"
        elif "global_trades" in self.sources:
            via = "global_trades"
        else:
            via = "goldsky"
        return CandidateWallet(
            wallet_address=self.wallet_address,
            discovered_via=via,
            initial_signal=round(self.initial_signal, 4),
            sample_market=self.sample_market,
        )
