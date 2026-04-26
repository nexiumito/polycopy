"""Pipeline de filtres du Strategy Engine.

Ordre fixe : MarketFilter → PositionSizer → SlippageChecker → RiskManager.
Premier rejet = arrêt du pipeline. Tous OK = `OrderApproved`.

Pas d'abstraction `AbstractFilter` à M2 — 4 classes concrètes (rule of three
pas encore atteinte, cf. `CLAUDE.md`).

M11 : ``SlippageChecker`` accepte un ``ws_client`` optionnel qui sert de
cache mid-price court-circuitant le HTTP ``/midpoint`` quand il est
disponible. Backward-compat absolue si ``ws_client=None`` → comportement
M2..M10 strict.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.models import MyPosition, TargetTrader
from polycopy.strategy.clob_read_client import ClobReadClient
from polycopy.strategy.dtos import FilterResult, MarketMetadata, PipelineContext
from polycopy.strategy.gamma_client import GammaApiClient

if TYPE_CHECKING:
    from polycopy.config import Settings
    from polycopy.executor.fee_rate_client import FeeRateClient
    from polycopy.storage.repositories import TradeLatencyRepository
    from polycopy.strategy.clob_ws_client import ClobMarketWSClient

log = structlog.get_logger(__name__)


class TraderLifecycleFilter:
    """Bloque les BUY pour les wallets en ``sell_only`` (M5_bis Phase C.4).

    Les SELL passent toujours — c'est le contrat wind-down : le bot
    continue à copier les SELL du wallet source pour fermer naturellement
    les positions ouvertes (pas de force-close, spec §9).

    Fast path : si ``EVICTION_ENABLED=false``, le filtre retourne
    instantanément ``passed=True`` sans query DB — zéro coût runtime en
    configuration M5 stricte.

    Placement pipeline : **premier** filtre (avant MarketFilter), car un
    BUY rejeté côté lifecycle n'a pas besoin d'appeler Gamma ni le
    midpoint CLOB. Coût nominal = 1 query indexée sur
    ``target_traders.wallet_address`` (index unique M1).
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._sf = session_factory
        self._settings = settings

    async def check(self, ctx: PipelineContext) -> FilterResult:
        if not self._settings.eviction_enabled:
            return FilterResult(passed=True)
        if ctx.trade.side == "SELL":
            return FilterResult(passed=True)
        # BUY : lookup wallet status (normalisation lowercase cohérente).
        async with self._sf() as session:
            stmt = select(TargetTrader.status).where(
                TargetTrader.wallet_address == ctx.trade.target_wallet.lower(),
            )
            status = (await session.execute(stmt)).scalar_one_or_none()
        if status == "sell_only":
            return FilterResult(passed=False, reason="wallet_sell_only")
        if status == "blacklisted":
            return FilterResult(passed=False, reason="wallet_blacklisted")
        return FilterResult(passed=True)


class MarketFilter:
    """Vérifie via Gamma que le marché est tradable et a une expiration assez lointaine."""

    def __init__(self, gamma_client: GammaApiClient, settings: Settings) -> None:
        self._gamma = gamma_client
        self._settings = settings

    async def check(self, ctx: PipelineContext) -> FilterResult:
        market = await self._gamma.get_market(ctx.trade.condition_id)
        if market is None:
            return FilterResult(passed=False, reason="market_not_found")
        ctx.market = market
        if market.active is False:
            return FilterResult(passed=False, reason="market_inactive")
        if market.closed or market.archived:
            return FilterResult(passed=False, reason="market_closed")
        if market.accepting_orders is False or market.enable_order_book is False:
            return FilterResult(passed=False, reason="orderbook_disabled")
        if (market.liquidity_clob or 0.0) < self._settings.min_market_liquidity_usd:
            return FilterResult(passed=False, reason="liquidity_too_low")
        end = self._resolve_end_datetime(market.end_date, market.end_date_iso)
        if end is not None:
            hours = (end - datetime.now(tz=UTC)).total_seconds() / 3600.0
            if hours < self._settings.min_hours_to_expiry:
                return FilterResult(passed=False, reason="expiry_too_close")
        return FilterResult(passed=True)

    @staticmethod
    def _resolve_end_datetime(
        end_date: datetime | None,
        end_date_iso: str | None,
    ) -> datetime | None:
        if end_date is not None:
            return end_date if end_date.tzinfo else end_date.replace(tzinfo=UTC)
        if end_date_iso is None:
            return None
        # "YYYY-MM-DD" → fin de journée UTC ; sinon parse ISO direct.
        try:
            if len(end_date_iso) == 10:  # YYYY-MM-DD
                return datetime.fromisoformat(end_date_iso + "T23:59:59+00:00")
            parsed = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None


class EntryPriceFilter:
    """Rejette les BUY dont le prix source dépasse ``strategy_max_entry_price``.

    Un BUY à ~1.00 = aucune upside (payoff max − coût = 0) et risque
    non-nul de dévaluation avant résolution. Seuil par défaut 0.97 →
    upside résiduel ≥ 3%. SELL jamais concerné — un wallet source qui
    sort à 0.99 veut simplement clôturer au meilleur prix et on doit
    pouvoir copier pour fermer notre position.

    Placement pipeline : après ``MarketFilter`` (sémantiquement : on sait
    d'abord que le marché est tradable, puis on regarde son prix). Coût
    runtime négligeable — une comparaison float.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def check(self, ctx: PipelineContext) -> FilterResult:
        if ctx.trade.side == "SELL":
            return FilterResult(passed=True)
        if ctx.trade.price > self._settings.strategy_max_entry_price:
            return FilterResult(passed=False, reason="entry_price_too_high")
        return FilterResult(passed=True)


class PositionSizer:
    """Calcule `my_size` selon `COPY_RATIO` plafonné à `MAX_POSITION_USD`.

    M13 Bug 5 : logique side-aware. Un BUY check *coarse* sur ``condition_id``
    (empêche double-buy YES/NO sur la même condition, préserve le
    comportement M2..M12). Un SELL check *fin* sur ``(condition_id,
    asset_id)`` pour autoriser la fermeture de la position exacte — sinon
    les SELL copiés restent bloqués indéfiniment et le capital ne se libère
    jamais en dry-run (cf. spec M13 §5.1).

    M16 : accepte un ``fee_rate_client`` optionnel pour le calcul EV
    post-fee dans ``_check_buy``. Si ``None`` ou si
    ``settings.strategy_fees_aware_enabled=False`` → comportement strict
    M13 (pas de fee adjustment). La logique réelle est branchée en MC.2.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        fee_rate_client: FeeRateClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._fee_rate_client = fee_rate_client

    async def check(self, ctx: PipelineContext) -> FilterResult:
        if ctx.trade.side == "BUY":
            return await self._check_buy(ctx)
        return await self._check_sell(ctx)

    async def _check_buy(self, ctx: PipelineContext) -> FilterResult:
        # M17 MD.1 : ségrégation live ↔ virtual sur (closed_at IS NULL).
        # Sans ce filtre, un flip dry_run → live hérite des positions virtuelles
        # M13 traînantes → tous les BUY live sont rejetés `position_already_open`
        # (audit C-001). Le pattern strict copié de `MyPositionRepository.get_open`
        # / `list_open_virtual` (qui filtrent déjà simulated).
        simulated_value = self._settings.execution_mode != "live"
        async with self._session_factory() as session:
            stmt = select(MyPosition).where(
                MyPosition.condition_id == ctx.trade.condition_id,
                MyPosition.closed_at.is_(None),
                MyPosition.simulated.is_(simulated_value),
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return FilterResult(passed=False, reason="position_already_open")
        raw_size = ctx.trade.size * self._settings.copy_ratio
        cap_size = self._settings.max_position_usd / ctx.trade.price if ctx.trade.price > 0 else 0.0
        raw_my_size = min(raw_size, cap_size)
        if raw_my_size <= 0:
            return FilterResult(passed=False, reason="size_zero")

        # M16 : fee adjustment EV-aware (opt-in via STRATEGY_FEES_AWARE_ENABLED).
        # Si flag off OU pas de fee_rate_client injecté → comportement strict
        # M2..M15 préservé (rétrocompat tests).
        if self._fee_rate_client is None or not self._settings.strategy_fees_aware_enabled:
            # M15 MB.6 : probation 0.25× kick in même si M16 fees off.
            raw_my_size = self._apply_probation_multiplier(raw_my_size, ctx)
            ctx.my_size = raw_my_size
            return FilterResult(passed=True)

        # Fetch base_fee (cache 60s, single-flight, fallback 1.80% si réseau down).
        # `base_fee` du endpoint est un FLAG binaire (cf. docstring FeeRateClient
        # + spec §11.5) : >0 = fee-enabled, =0 = fee-free. Court-circuit propre
        # si =0 — pas de calcul formule sur un marché sans fee.
        base_fee_rate = await self._fee_rate_client.get_fee_rate(ctx.trade.asset_id)
        if base_fee_rate == Decimal("0"):
            ctx.fee_rate = 0.0
            ctx.fee_cost_usd = 0.0
            # Pas d'EV calculation côté polycopy si pas de fee — laisse passer.
            ctx.my_size = raw_my_size
            return FilterResult(passed=True)

        # Marché fee-enabled : calcul effective rate via formule Polymarket
        # officielle, paramétrée par `market.fee_type` Gamma.
        effective_fee_rate = self._compute_effective_fee_rate(
            price=Decimal(str(ctx.trade.price)),
            market=ctx.market,
        )

        notional = Decimal(str(raw_my_size)) * Decimal(str(ctx.trade.price))
        fee_cost = notional * effective_fee_rate
        # Approximation EV polycopy (cf. spec §11.4 : choix simple vs Bayésien).
        # `expected_max_gain` = payoff max si YES wins = my_size × (1 - price).
        # C'est le plafond d'upside, pas une vraie EV Bayésienne. Couplé au seuil
        # `STRATEGY_MIN_EV_USD_AFTER_FEE`, ça rejette les BUYs où l'upside max
        # ne couvre même pas la fee + un slack minimal.
        expected_max_gain = Decimal(str(raw_my_size)) * (
            Decimal("1.0") - Decimal(str(ctx.trade.price))
        )
        ev_after_fee = expected_max_gain - fee_cost

        ctx.fee_rate = float(effective_fee_rate)
        ctx.fee_cost_usd = float(fee_cost)
        ctx.ev_after_fee_usd = float(ev_after_fee)

        if ev_after_fee < self._settings.strategy_min_ev_usd_after_fee:
            log.debug(
                "ev_negative_after_fees",
                tx_hash=ctx.trade.tx_hash,
                price=ctx.trade.price,
                raw_my_size=raw_my_size,
                effective_fee_rate=str(effective_fee_rate),
                fee_cost=str(fee_cost),
                ev_after_fee=str(ev_after_fee),
                threshold=str(self._settings.strategy_min_ev_usd_after_fee),
            )
            return FilterResult(passed=False, reason="ev_negative_after_fees")

        # M15 MB.6 : probation 0.25× appliqué APRÈS le fee/EV check (la
        # probation est un sizing layer, pas un EV gate — on ne re-rejette
        # pas un trade probation pour fee insuffisant relatif à la size
        # 0.25×, le check EV s'applique à la raw size).
        raw_my_size = self._apply_probation_multiplier(raw_my_size, ctx)

        ctx.my_size = raw_my_size
        return FilterResult(passed=True)

    def _apply_probation_multiplier(
        self,
        raw_my_size: float,
        ctx: PipelineContext,
    ) -> float:
        """M15 MB.6 — multiplie ``raw_my_size`` par
        ``probation_size_multiplier`` (default 0.25) si
        ``ctx.trade.is_source_probation == True``.

        Pure (lit ``self._settings`` + ``ctx.trade.is_source_probation`` ;
        retourne le size probationné). Wallet non-probation → no-op
        (multiplier = 1.0 mathématiquement, court-circuit explicite pour
        clarity).
        """
        if not ctx.trade.is_source_probation:
            return raw_my_size
        multiplier = float(self._settings.probation_size_multiplier)
        return raw_my_size * multiplier

    @staticmethod
    def _compute_effective_fee_rate(
        *,
        price: Decimal,
        market: MarketMetadata | None,
    ) -> Decimal:
        """Calcule l'effective fee rate via formule Polymarket officielle.

        ``fee = C × p × feeRate × (p × (1-p))^exponent`` →
        ``effective_rate = feeRate × (p × (1-p))^exponent`` (ratio fee/notional).

        Mapping ``market.fee_type`` → ``(feeRate_param, exponent)`` :

        - ``crypto_fees_v2`` : (0.25, 2) — max 1.56 % à p=0.5
        - ``sports_fees_v2`` (post-March 30 2026, doc Polymarket live) :
          (0.03, 1) — peak 0.75 % à p=0.5. ``sports_fees_v1`` (NCAAB/Serie A
          pré-rollout) : (0.0175, 1) — peak 0.44 %. On groupe tous via
          ``startswith("sports_fees")`` avec params v2 (worst case).
        - autre / unknown / market None : fallback **conservateur Crypto**
          (0.25, 2). Mieux sur-estimer fee que l'inverse (asymétrie d'impact).

        Si ``market`` est None ou ``fee_type`` est None / "" → fallback Crypto.

        Note : on ne court-circuite PAS sur ``market.fees_enabled=False`` car
        certains markets pré-rollout ont ``fees_enabled=null`` mais sont
        fee-free en réalité. Le `base_fee=0` du `/fee-rate` endpoint sert
        de short-circuit upstream (fee_rate=0 → fee_cost=0 → pas de rejet).
        """
        fee_type = (market.fee_type if market is not None else None) or ""
        if fee_type == "crypto_fees_v2":
            fee_rate_param, exponent = Decimal("0.25"), 2
        elif fee_type.startswith("sports_fees"):
            # Post-March 30 2026 rollout : feeRate=0.03 (vs 0.0175 pré-rollout
            # NCAAB/Serie A). Source : docs Polymarket live 2026-04-25.
            fee_rate_param, exponent = Decimal("0.03"), 1
        else:
            # Inconnu (politics_fees_v*, finance_fees_v*, etc. à venir) →
            # conservateur (Crypto formula).
            fee_rate_param, exponent = Decimal("0.25"), 2

        p_one_minus_p = price * (Decimal("1") - price)
        return fee_rate_param * (p_one_minus_p**exponent)

    async def _check_sell(self, ctx: PipelineContext) -> FilterResult:
        # Match fin (condition_id, asset_id) : un SELL YES ne ferme pas une
        # position NO. Sizing proportional capé à ``existing.size`` (on ne
        # vend jamais plus qu'on détient). ``max_position_usd`` N/A en SELL.
        # M17 MD.1 : filtre simulated cohérent avec _check_buy (audit C-001).
        simulated_value = self._settings.execution_mode != "live"
        async with self._session_factory() as session:
            stmt = select(MyPosition).where(
                MyPosition.condition_id == ctx.trade.condition_id,
                MyPosition.asset_id == ctx.trade.asset_id,
                MyPosition.closed_at.is_(None),
                MyPosition.simulated.is_(simulated_value),
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            return FilterResult(passed=False, reason="sell_without_position")
        raw_size = ctx.trade.size * self._settings.copy_ratio
        ctx.my_size = min(raw_size, float(existing.size or 0.0))
        if ctx.my_size <= 0:
            return FilterResult(passed=False, reason="size_zero")
        return FilterResult(passed=True)


class SlippageChecker:
    """Compare le mid CLOB courant au prix source ; rejette si > `MAX_SLIPPAGE_PCT`.

    M11 : lookup du cache WS EN PREMIER (si ``ws_client`` fourni ET
    ``strategy_clob_ws_enabled``) ; fallback HTTP ``/midpoint`` si WS
    indisponible (flag off, token jamais vu, WS down, cache stale). Le
    contrat ``FilterResult(passed=False, reason="no_orderbook")`` est
    strictement préservé quand WS ET HTTP retournent ``None``.
    """

    def __init__(
        self,
        clob_client: ClobReadClient,
        settings: Settings,
        ws_client: ClobMarketWSClient | None = None,
    ) -> None:
        self._clob = clob_client
        self._settings = settings
        self._ws = ws_client

    async def check(self, ctx: PipelineContext) -> FilterResult:
        mid: float | None = None
        if self._ws is not None and self._settings.strategy_clob_ws_enabled:
            await self._ws.subscribe(ctx.trade.asset_id)
            mid = await self._ws.get_mid_price(ctx.trade.asset_id)
        if mid is None:
            mid = await self._clob.get_midpoint(ctx.trade.asset_id)
        if mid is None:
            return FilterResult(passed=False, reason="no_orderbook")
        ctx.midpoint = mid
        if ctx.trade.price <= 0:
            return FilterResult(passed=False, reason="invalid_source_price")
        slippage = abs(mid - ctx.trade.price) / ctx.trade.price
        ctx.slippage_pct = slippage * 100.0
        if ctx.slippage_pct > self._settings.max_slippage_pct:
            return FilterResult(passed=False, reason="slippage_exceeded")
        return FilterResult(passed=True)


class RiskManager:
    """Vérifie capital disponible, exposition totale et drawdown."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def check(self, ctx: PipelineContext) -> FilterResult:
        if ctx.my_size is None or ctx.midpoint is None:
            return FilterResult(passed=False, reason="risk_inputs_missing")
        # M17 MD.1 : exposition calculée uniquement sur les positions du mode
        # courant (audit C-001). Sans ce filtre, le live additionne les
        # positions virtuelles M13 dans le calcul d'exposition → faux-positif
        # `capital_exceeded` au flip.
        simulated_value = self._settings.execution_mode != "live"
        async with self._session_factory() as session:
            stmt = select(MyPosition).where(
                MyPosition.closed_at.is_(None),
                MyPosition.simulated.is_(simulated_value),
            )
            open_positions = list((await session.execute(stmt)).scalars().all())
        current_exposure = sum((p.size or 0.0) * (p.avg_price or 0.0) for p in open_positions)
        prospective_cost = ctx.my_size * ctx.midpoint
        if current_exposure + prospective_cost > self._settings.risk_available_capital_usd_stub:
            return FilterResult(passed=False, reason="capital_exceeded")
        # Drawdown : `pnl_snapshots` est vide à M2 → drawdown = 0%.
        return FilterResult(passed=True)


# Map filtre → stage name (cf. spec M11 §5.2). SlippageChecker est absent :
# son coût est inclus dans le stage cumulatif `strategy_filtered_ms` mesuré
# par l'orchestrator autour de `run_pipeline`, pas dans un stage propre.
_STAGE_BY_FILTER: dict[str, str] = {
    "MarketFilter": "strategy_enriched_ms",
    "PositionSizer": "strategy_sized_ms",
    "RiskManager": "strategy_risk_checked_ms",
}


async def run_pipeline(
    trade: DetectedTradeDTO,
    *,
    gamma_client: GammaApiClient,
    clob_client: ClobReadClient,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    ws_client: ClobMarketWSClient | None = None,
    latency_repo: TradeLatencyRepository | None = None,
    fee_rate_client: FeeRateClient | None = None,
) -> tuple[Literal["APPROVED", "REJECTED"], str | None, PipelineContext]:
    """Exécute les 4 filtres en séquence. Premier rejet = arrêt.

    M11 : ``ws_client`` alimente ``SlippageChecker`` (cache mid-price) ;
    ``latency_repo`` persiste les échantillons ``strategy_enriched_ms`` /
    ``strategy_sized_ms`` / ``strategy_risk_checked_ms`` quand
    ``settings.latency_instrumentation_enabled=True`` ET
    ``trade.trade_id is not None``.

    M16 : ``fee_rate_client`` (opt-in via ``STRATEGY_FEES_AWARE_ENABLED``)
    alimente ``PositionSizer._check_buy`` pour le calcul EV post-fee.
    Si ``None`` → comportement strict M2..M15 (pas de fee adjustment).
    """
    ctx = PipelineContext(trade=trade)
    filters = (
        ("TraderLifecycleFilter", TraderLifecycleFilter(session_factory, settings)),
        ("MarketFilter", MarketFilter(gamma_client, settings)),
        ("EntryPriceFilter", EntryPriceFilter(settings)),
        ("PositionSizer", PositionSizer(session_factory, settings, fee_rate_client)),
        ("SlippageChecker", SlippageChecker(clob_client, settings, ws_client)),
        ("RiskManager", RiskManager(session_factory, settings)),
    )
    instrumented = settings.latency_instrumentation_enabled and trade.trade_id is not None
    for name, f in filters:
        stage_name = _STAGE_BY_FILTER.get(name)
        if instrumented and stage_name is not None:
            t0 = time.perf_counter_ns()
            result = await f.check(ctx)
            duration_ms = (time.perf_counter_ns() - t0) / 1e6
            log.info(
                "stage_complete",
                stage_name=stage_name,
                stage_duration_ms=round(duration_ms, 3),
                filter_name=name,
                passed=result.passed,
            )
            if latency_repo is not None and trade.trade_id is not None:
                await latency_repo.insert(trade.trade_id, stage_name, duration_ms)
        else:
            result = await f.check(ctx)
        ctx.record_filter(name, result)
        if not result.passed:
            return "REJECTED", result.reason, ctx
    return "APPROVED", None, ctx
