"""Orchestrator du cycle M5 (découverte + scoring + décisions).

Pattern analogue à ``PnlSnapshotWriter`` (M4) et ``DashboardOrchestrator``
(M4.5) : une seule coroutine `run_forever(stop_event)` schedulée dans le
`asyncio.TaskGroup` de ``__main__``, sleep interruptible via ``stop_event``.

Zéro instanciation si ``settings.discovery_enabled=False`` — garde dans
``__main__`` (cf. spec §2.6).

**M12 — scoring v2 dual-compute** : quand ``SCORING_VERSION=v1`` (default)
ET ``SCORING_V2_SHADOW_DAYS > 0``, l'orchestrator calcule **en parallèle** un
score v2 par wallet (via :class:`MetricsCollectorV2` + :func:`compute_score_v2`)
qu'il écrit dans ``trader_scores`` avec ``scoring_version='v2'`` — mais qui
**ne pilote pas** ``DecisionEngine`` (invariant lifecycle M5 strict tant que
``SCORING_VERSION=v1``). Les gates v2 s'appliquent en amont : wallet rejeté =
`trader_events.event_type="gate_rejected"` + skip scoring v2 (v1 continue).
Le scheduler :class:`TraderDailyPnlWriter` est co-lancé dans un TaskGroup
interne pour produire l'equity curve nécessaire à Sortino/Calmar.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import AsyncExitStack, nullcontext
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import structlog
from sqlalchemy import func, select

from polycopy.discovery.candidate_pool import CandidatePool
from polycopy.discovery.data_api_client import DiscoveryDataApiClient
from polycopy.discovery.decision_engine import DecisionEngine
from polycopy.discovery.dtos import DiscoveryDecision, ScoringResult
from polycopy.discovery.eviction import (
    TRANSITION_TO_EVENT_TYPE,
    EvictionDecision,
    EvictionScheduler,
)
from polycopy.discovery.goldsky_client import GoldskyClient
from polycopy.discovery.metrics_collector import MetricsCollector
from polycopy.discovery.metrics_collector_v2 import MetricsCollectorV2
from polycopy.discovery.scoring import compute_score
from polycopy.discovery.scoring.v2 import (
    MarketCategoryResolver,
    PoolContext,
    TraderMetricsV2,
    bind_pool_context,
    check_all_gates,
    compute_score_v2,
)
from polycopy.discovery.trader_daily_pnl_writer import TraderDailyPnlWriter
from polycopy.monitoring.dtos import Alert
from polycopy.storage.dtos import TraderEventDTO, TraderScoreDTO
from polycopy.storage.models import TraderScore
from polycopy.storage.repositories import (
    TargetTraderRepository,
    TraderDailyPnlRepository,
    TraderEventRepository,
    TraderScoreRepository,
)
from polycopy.strategy.gamma_client import GammaApiClient

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from polycopy.config import Settings

log = structlog.get_logger(__name__)


# Mapping DiscoveryDecision.decision → TraderEvent.event_type (stockage).
_DECISION_TO_EVENT_TYPE = {
    "discovered_shadow": "discovered",
    "promote_active": "promoted_active",
    "demote_paused": "demoted_paused",  # legacy : jamais produit par M5_bis
    "demote_shadow": "demoted_to_shadow",  # M5_bis : fusion avec shadow + flag UX
    "keep": "kept",
    "skip_blacklist": "skipped_blacklist",
    "skip_cap": "skipped_cap",
    "revived_shadow": "revived_shadow",
}


class DiscoveryOrchestrator:
    """Coroutine TaskGroup : cycle scoring toutes les `DISCOVERY_INTERVAL_SECONDS`."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        alerts_queue: asyncio.Queue[Alert] | None = None,
    ) -> None:
        self._sf = session_factory
        self._settings = settings
        self._alerts = alerts_queue

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Boucle jusqu'à `stop_event` set. Sleep interruptible entre 2 cycles."""
        cfg = self._settings
        log.info(
            "discovery_starting",
            interval_s=cfg.discovery_interval_seconds,
            pool_size=cfg.discovery_candidate_pool_size,
            backend=cfg.discovery_backend,
            scoring_version=cfg.scoring_version,
            scoring_v2_shadow_days=cfg.scoring_v2_shadow_days,
            max_active_traders=cfg.max_active_traders,
            trader_shadow_days=cfg.trader_shadow_days,
            trader_daily_pnl_enabled=cfg.trader_daily_pnl_enabled,
        )
        if cfg.trader_shadow_days == 0 and cfg.discovery_shadow_bypass:
            log.warning(
                "discovery_shadow_bypass_enabled",
                reason="auto_promote_immediate",
            )
        if cfg.scoring_v2_cold_start_mode:
            log.warning(
                "scoring_v2_cold_start_mode_enabled",
                reason="trade_count_90d_gate_relaxed_to_20",
            )

        async with httpx.AsyncClient() as http_client:
            data_api = DiscoveryDataApiClient(http_client)
            gamma = GammaApiClient(http_client)
            goldsky: GoldskyClient | None = None
            if cfg.discovery_backend in ("goldsky", "hybrid"):
                goldsky = GoldskyClient(http_client, cfg)
            candidate_pool = CandidatePool(data_api, gamma, goldsky, cfg)
            metrics_collector = MetricsCollector(data_api, cfg)
            target_repo = TargetTraderRepository(self._sf)
            score_repo = TraderScoreRepository(self._sf)
            event_repo = TraderEventRepository(self._sf)
            daily_pnl_repo = TraderDailyPnlRepository(self._sf)
            decision_engine = DecisionEngine(target_repo, cfg, self._alerts)

            # M5_bis Phase C : EvictionScheduler opt-in strict.
            eviction_scheduler: EvictionScheduler | None = None
            if cfg.eviction_enabled:
                eviction_scheduler = EvictionScheduler(
                    target_repo=target_repo,
                    session_factory=self._sf,
                    settings=cfg,
                    alerts_queue=self._alerts,
                )
                # Reconcile blacklist au boot (idempotent, applique T10 aux
                # wallets déjà en DB qui sont maintenant dans
                # BLACKLISTED_WALLETS, et T11/T12 pour ceux qui en sortent).
                boot_decisions = await eviction_scheduler.reconcile_blacklist()
                for decision in boot_decisions:
                    await self._persist_eviction_event(event_repo, decision, cfg)
                log.info(
                    "eviction_scheduler_started",
                    boot_reconcile_decisions=len(boot_decisions),
                    score_margin=cfg.eviction_score_margin,
                    hysteresis_cycles=cfg.eviction_hysteresis_cycles,
                    max_sell_only=cfg.max_sell_only_wallets,
                )

            # M12 : sur-composants scoring v2 (créés inconditionnellement,
            # utilisés seulement si v2 pilote OU shadow_days > 0 — cf.
            # _should_compute_v2 ci-dessous).
            category_resolver = MarketCategoryResolver(http_client)
            metrics_collector_v2 = MetricsCollectorV2(
                base_collector=metrics_collector,
                daily_pnl_repo=daily_pnl_repo,
                data_api=data_api,
                category_resolver=category_resolver,
                settings=cfg,
            )
            daily_pnl_writer = TraderDailyPnlWriter(
                data_api=data_api,
                target_repo=target_repo,
                daily_pnl_repo=daily_pnl_repo,
                settings=cfg,
            )

            # TaskGroup interne : co-lance TraderDailyPnlWriter si enabled.
            # `AsyncExitStack` + `nullcontext` pour rester simple si writer off.
            async with AsyncExitStack() as stack:
                if cfg.trader_daily_pnl_enabled:
                    tg = await stack.enter_async_context(asyncio.TaskGroup())
                    tg.create_task(daily_pnl_writer.run_forever(stop_event))
                else:
                    # Placeholder typé pour garder le pattern homogène.
                    await stack.enter_async_context(nullcontext())

                log.info("discovery_started")

                while not stop_event.is_set():
                    try:
                        await self._run_one_cycle(
                            candidate_pool,
                            metrics_collector,
                            metrics_collector_v2,
                            score_repo,
                            event_repo,
                            target_repo,
                            decision_engine,
                            eviction_scheduler,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("discovery_cycle_failed")
                        self._push_alert(
                            Alert(
                                level="ERROR",
                                event="discovery_cycle_failed",
                                body=("Discovery cycle raised an exception, see structured logs."),
                                cooldown_key="discovery_cycle_failed",
                            ),
                        )
                        # Retry rapide après un crash (1 min) avant le sleep normal.
                        if await _sleep_or_stop(stop_event, 60.0):
                            break
                        continue
                    if await _sleep_or_stop(
                        stop_event,
                        float(cfg.discovery_interval_seconds),
                    ):
                        break
        log.info("discovery_stopped")

    async def _run_one_cycle(
        self,
        candidate_pool: CandidatePool,
        metrics_collector: MetricsCollector,
        metrics_collector_v2: MetricsCollectorV2,
        score_repo: TraderScoreRepository,
        event_repo: TraderEventRepository,
        target_repo: TargetTraderRepository,
        decision_engine: DecisionEngine,
        eviction_scheduler: EvictionScheduler | None = None,
    ) -> None:
        cycle_at = datetime.now(tz=UTC)
        log.info("discovery_cycle_started", cycle_at=cycle_at.isoformat())
        t0 = time.monotonic()

        # 1. Snapshot `status='paused'` pour les exclure du pool (respect user).
        paused = await target_repo.list_by_status("paused")
        exclude = {p.wallet_address for p in paused}

        # 2. Construction du pool de candidats.
        candidates = await candidate_pool.build(exclude_wallets=exclude)

        # 3. Merge avec les traders existants (re-score tous sauf pinned).
        existing = await target_repo.list_all()
        existing_by_wallet = {t.wallet_address: t for t in existing}
        candidate_wallets = {c.wallet_address for c in candidates}
        # On ne re-score pas les pinned : score reste figé à None (ils sont
        # whitelistés, pas évalués — le user les pilote manuellement).
        to_score = list(
            candidate_wallets | {t.wallet_address for t in existing if not t.pinned},
        )

        # 4. Snapshot du count active pour le cap.
        active_count_start = await target_repo.count_by_status("active")
        # Promotions vont incrémenter active_count au fil du scoring — on l'estime
        # côté engine à chaque décide() via ce compteur mutable partagé.
        promotions = demotions = kept = skipped = discovered = 0
        gate_rejected_count = 0
        v2_scored_count = 0
        active_count = active_count_start

        # M5_bis Phase C : scores par wallet passés à EvictionScheduler en aval.
        scores_by_wallet: dict[str, float] = {}

        # M12 — décide si v2 doit être calculé (pilote OU shadow actif).
        compute_v2 = await self._should_compute_v2()
        is_v2_pilot = self._settings.scoring_version == "v2"

        # M12 — pre-build PoolContext si v2 impliqué (1 appel MetricsCollectorV2
        # par wallet × fetch metrics, puis agrégation pool-wide).
        metrics_v2_by_wallet: dict[str, TraderMetricsV2] = {}
        pool_context: PoolContext | None = None
        if compute_v2 and to_score:
            metrics_v2_by_wallet = await self._collect_metrics_v2_batch(
                to_score,
                metrics_collector_v2,
            )
            pool_context = self._build_pool_context_from_metrics(
                metrics_v2_by_wallet.values(),
            )

        # 5. Scoring séquentiel par wallet (le client HTTP a son propre semaphore).
        # Le `bind_pool_context` est posé sur tout le bloc — safe même si
        # `pool_context is None` (le contextvar reste à None, le wrapper v2
        # renvoie 0.0 + log warn).
        with bind_pool_context(pool_context):
            for wallet in to_score:
                try:
                    metrics = await metrics_collector.collect(wallet)
                except Exception:
                    log.exception("metrics_collect_failed", wallet=wallet)
                    continue

                # --- v2 path (gates + score si compute_v2) ------------------
                metrics_v2 = metrics_v2_by_wallet.get(wallet) if compute_v2 else None
                gate_rejected = False
                score_v2_value: float | None = None
                v2_breakdown = None
                if metrics_v2 is not None and pool_context is not None:
                    gates = check_all_gates(metrics_v2, wallet, self._settings)
                    if not gates.passed:
                        gate_rejected = True
                        gate_rejected_count += 1
                        await self._write_gate_rejected_event(
                            event_repo=event_repo,
                            wallet=wallet,
                            current=existing_by_wallet.get(wallet),
                            gates=gates,
                        )
                    else:
                        v2_breakdown = compute_score_v2(metrics_v2, pool_context)
                        score_v2_value = v2_breakdown.score

                # Si v2 est pilote ET gate rejected → skip scoring complet
                # (pas de row trader_scores, pas de decision_engine). v1
                # continue seulement si v2 n'est pas pilote (shadow mode).
                if is_v2_pilot and gate_rejected:
                    log.debug("gate_rejected_pilot_skip", wallet=wallet)
                    continue

                # --- v1 path (intact, signature M5) -------------------------
                score_v1_value, low_conf = compute_score(
                    metrics,
                    settings=self._settings,
                )

                # Version pilote = écrire target_traders.score (colonne
                # overwrite M5) + la row trader_scores pilote.
                pilot_score = score_v2_value if is_v2_pilot else score_v1_value
                pilot_version = "v2" if is_v2_pilot else "v1"
                # Cas limite : v2 pilote ET pas de score (e.g. compute_v2=False
                # si shadow=0 + scoring_version=v2 + first cycle without
                # pool_context). On fallback au score v1 comme filet de
                # sécurité (jamais laisser un cycle sans piloter la décision).
                if pilot_score is None:
                    pilot_score = score_v1_value
                    pilot_version = "v1"

                current = existing_by_wallet.get(wallet)

                # Persist v1 row si trader existe et pas rejeté par v2 en
                # mode pilote v1 (shadow mode = v1 garde sa trace).
                if current is not None:
                    await score_repo.insert(
                        TraderScoreDTO(
                            target_trader_id=current.id,
                            wallet_address=wallet,
                            score=score_v1_value,
                            scoring_version="v1",
                            low_confidence=low_conf,
                            metrics_snapshot=metrics.model_dump(mode="json"),
                        ),
                    )
                    # Shadow v2 : double-write trader_scores row v2 pour audit.
                    if (
                        v2_breakdown is not None
                        and metrics_v2 is not None
                        and score_v2_value is not None
                    ):
                        await score_repo.insert(
                            TraderScoreDTO(
                                target_trader_id=current.id,
                                wallet_address=wallet,
                                score=score_v2_value,
                                scoring_version="v2",
                                low_confidence=False,
                                metrics_snapshot={
                                    "base": metrics.model_dump(mode="json"),
                                    "v2_raw": v2_breakdown.raw.model_dump(
                                        mode="json",
                                    ),
                                    "v2_normalized": v2_breakdown.normalized.model_dump(
                                        mode="json",
                                    ),
                                    "brier_baseline_pool": (v2_breakdown.brier_baseline_pool),
                                },
                            ),
                        )
                        v2_scored_count += 1
                    # target_traders.score (colonne overwrite M5) reflète la
                    # version pilote.
                    await target_repo.update_score(
                        wallet,
                        score=pilot_score,
                        scoring_version=pilot_version,
                        scored_at=cycle_at,
                    )

                scoring = ScoringResult(
                    wallet_address=wallet,
                    score=pilot_score,
                    scoring_version=pilot_version,
                    low_confidence=low_conf if pilot_version == "v1" else False,
                    metrics=metrics,
                    cycle_at=cycle_at,
                )
                scores_by_wallet[wallet.lower()] = pilot_score

                decision = await decision_engine.decide(
                    scoring,
                    current,
                    active_count=active_count,
                )
                await self._persist_event(
                    event_repo,
                    decision,
                    scoring,
                    current,
                    cycle_at,
                )
                # Compteurs
                if decision.decision == "promote_active":
                    promotions += 1
                    active_count += 1
                    await self._push_promoted_alert(wallet, pilot_score)
                elif decision.decision in ("demote_paused", "demote_shadow"):
                    demotions += 1
                    active_count -= 1
                    await self._push_demoted_alert(wallet, pilot_score)
                elif decision.decision == "discovered_shadow":
                    discovered += 1
                elif decision.decision in ("keep", "revived_shadow"):
                    kept += 1
                elif decision.decision in ("skip_blacklist", "skip_cap"):
                    skipped += 1

                log.debug(
                    "score_computed",
                    wallet=wallet,
                    score_v1=round(score_v1_value, 4),
                    score_v2=(round(score_v2_value, 4) if score_v2_value is not None else None),
                    pilot=pilot_version,
                    low_confidence=low_conf,
                    decision=decision.decision,
                )

        # M5_bis Phase C : hook EvictionScheduler après la boucle M5.
        # Idempotent + flag off = no-op (scheduler non-instancié).
        eviction_applied = 0
        if eviction_scheduler is not None:
            try:
                # reconcile_blacklist en premier : si l'user a flippé un wallet
                # dans BLACKLISTED_WALLETS en warm reload, on l'aligne avant de
                # laisser le scheduler appliquer des transitions sur ce wallet.
                reconcile_decisions = await eviction_scheduler.reconcile_blacklist()
                for ev_decision in reconcile_decisions:
                    await self._persist_eviction_event(
                        event_repo,
                        ev_decision,
                        self._settings,
                    )
                cycle_decisions = await eviction_scheduler.run_cycle(scores_by_wallet)
                for ev_decision in cycle_decisions:
                    await self._persist_eviction_event(
                        event_repo,
                        ev_decision,
                        self._settings,
                    )
                eviction_applied = len(reconcile_decisions) + len(cycle_decisions)
            except Exception:
                # Isolation : un crash du scheduler eviction ne doit jamais
                # planter le cycle Discovery M5 principal (non-régression).
                log.exception("eviction_cycle_failed")

        duration_ms = int((time.monotonic() - t0) * 1000)
        log.info(
            "discovery_cycle_completed",
            candidates_seen=len(candidates),
            scored=len(to_score),
            discovered=discovered,
            promoted=promotions,
            demoted=demotions,
            kept=kept,
            skipped=skipped,
            gate_rejected=gate_rejected_count,
            v2_scored=v2_scored_count,
            active_count=active_count,
            eviction_applied=eviction_applied,
            duration_ms=duration_ms,
            scoring_version=self._settings.scoring_version,
        )

    async def _persist_eviction_event(
        self,
        event_repo: TraderEventRepository,
        decision: EvictionDecision,
        settings: Settings,
    ) -> None:
        """Persiste un EvictionDecision dans trader_events (audit trail).

        Mapping transition → event_type via TRANSITION_TO_EVENT_TYPE. Les
        defer_* sont persistés tels quels (audit only, pas de transition
        DB). Le champ ``event_metadata`` porte le payload riche M5_bis
        (delta, cycles_observed, triggering_wallet, reason_code).
        """
        event_type = TRANSITION_TO_EVENT_TYPE.get(decision.transition, "kept")
        metadata: dict[str, object] = {
            "transition": decision.transition,
            "reason_code": decision.reason_code,
        }
        if decision.delta_vs_worst_active is not None:
            metadata["delta_vs_worst_active"] = round(decision.delta_vs_worst_active, 4)
        if decision.triggering_wallet is not None:
            metadata["triggering_wallet"] = decision.triggering_wallet
        if decision.cycles_observed is not None:
            metadata["cycles_observed"] = decision.cycles_observed
        await event_repo.insert(
            TraderEventDTO(
                wallet_address=decision.wallet_address,
                event_type=event_type,
                from_status=decision.from_status,
                to_status=decision.to_status,
                score_at_event=decision.score_at_event,
                scoring_version=settings.scoring_version,
                reason=decision.reason_code,
                event_metadata=metadata,
            ),
        )

    async def _persist_event(
        self,
        event_repo: TraderEventRepository,
        decision: DiscoveryDecision,
        scoring: ScoringResult,
        current: object,
        cycle_at: datetime,
    ) -> None:
        del scoring, current, cycle_at  # réservés pour métadonnées enrichies future
        event_type = _DECISION_TO_EVENT_TYPE.get(decision.decision, "kept")
        await event_repo.insert(
            TraderEventDTO(
                wallet_address=decision.wallet_address,
                event_type=event_type,
                from_status=decision.from_status,
                to_status=decision.to_status,
                score_at_event=decision.score_at_event,
                scoring_version=decision.scoring_version,
                reason=decision.reason,
                event_metadata=decision.event_metadata or None,
            ),
        )

    async def _should_compute_v2(self) -> bool:
        """Détermine si v2 doit être calculé ce cycle (pilote OU shadow actif).

        Logique :

        - ``SCORING_VERSION=v2`` → toujours compute v2 (pilote).
        - ``SCORING_VERSION=v1`` + ``SCORING_V2_SHADOW_DAYS > 0`` → compute v2
          si la shadow period n'est pas encore expirée (detected via
          :meth:`_is_v2_shadow_active`).
        - Sinon → pas de compute v2 (pur M5).
        """
        cfg = self._settings
        if cfg.scoring_version == "v2":
            return True
        if cfg.scoring_v2_shadow_days <= 0:
            return False
        return await self._is_v2_shadow_active()

    async def _is_v2_shadow_active(self) -> bool:
        """Shadow period encore active ?

        Query DB : ``MIN(cycle_at) FROM trader_scores WHERE
        scoring_version='v2'``. Si aucune row v2 → shadow period pas encore
        démarrée, retourne True (on va la démarrer ce cycle). Si première row
        v2 < ``SCORING_V2_SHADOW_DAYS`` jours → shadow encore active.
        """
        cfg = self._settings
        async with self._sf() as session:
            stmt = select(func.min(TraderScore.cycle_at)).where(
                TraderScore.scoring_version == "v2",
            )
            first_v2_cycle = (await session.execute(stmt)).scalar_one_or_none()
        if first_v2_cycle is None:
            return True
        # SQLite ne persiste pas tzinfo → ré-injecte UTC si naïf.
        if first_v2_cycle.tzinfo is None:
            first_v2_cycle = first_v2_cycle.replace(tzinfo=UTC)
        elapsed = datetime.now(tz=UTC) - first_v2_cycle
        return elapsed < timedelta(days=cfg.scoring_v2_shadow_days)

    async def _collect_metrics_v2_batch(
        self,
        wallets: list[str],
        collector: MetricsCollectorV2,
    ) -> dict[str, TraderMetricsV2]:
        """Fetch metrics v2 pour chaque wallet. Séquentiel (Data API semaphore
        in-process). Les erreurs par wallet n'interrompent pas les autres.
        """
        out: dict[str, TraderMetricsV2] = {}
        for wallet in wallets:
            try:
                out[wallet] = await collector.collect(wallet)
            except Exception:
                log.exception("metrics_v2_collect_failed", wallet=wallet)
        return out

    def _build_pool_context_from_metrics(
        self,
        metrics_iter: object,  # Iterable[TraderMetricsV2] — évite import TYPE_CHECKING-only
    ) -> PoolContext:
        """Agrège les valeurs brutes pool-wide + Brier baseline pool.

        Pour chaque facteur : collect les valeurs "raw" (pré-normalisation) en
        ré-appelant les factors sur chaque metrics. Le :class:`PoolContext`
        est consommé ensuite par :func:`apply_pool_normalization` dans
        :func:`compute_score_v2`.

        Brier baseline pool = moyenne des ``brier_90d`` non-None du pool
        (approximation §3.3 simplifiée — un "wallet moyen" calibrerait comme
        le pool). Fallback 0.25 si aucun brier disponible.
        """
        from polycopy.discovery.scoring.v2.factors import (
            compute_calibration,
            compute_consistency,
            compute_discipline,
            compute_risk_adjusted,
            compute_specialization,
            compute_timing_alpha,
        )

        risk_adjusted_pool: list[float] = []
        calibration_pool: list[float] = []
        timing_alpha_pool: list[float] = []
        specialization_pool: list[float] = []
        consistency_pool: list[float] = []
        discipline_pool: list[float] = []
        brier_values: list[float] = []

        for m in metrics_iter:  # type: ignore[attr-defined]
            if not isinstance(m, TraderMetricsV2):
                continue
            if m.brier_90d is not None:
                brier_values.append(float(m.brier_90d))
            risk_adjusted_pool.append(compute_risk_adjusted(m))
            # Pour calibration : on calcule avec baseline provisoire (0.25)
            # — la vraie baseline sera injectée dans compute_score_v2 après.
            # Ici on collect le *raw brier* utile à la normalisation, pas la
            # skill finale. On dérive skill avec baseline finale plus tard.
            # Compromis : on stocke le pool de brier raw + la baseline
            # séparément — la normalisation des calibrations finales se fait
            # sur les *skill scores* finaux, mais on peut approximer avec
            # un remap ici.
            calibration_pool.append(
                compute_calibration(m, brier_baseline_pool=0.25),
            )
            timing_alpha_pool.append(compute_timing_alpha(m))
            specialization_pool.append(compute_specialization(m))
            consistency_pool.append(compute_consistency(m))
            discipline_pool.append(compute_discipline(m))

        brier_baseline_pool = sum(brier_values) / len(brier_values) if brier_values else 0.25
        return PoolContext(
            risk_adjusted_pool=risk_adjusted_pool,
            calibration_pool=calibration_pool,
            timing_alpha_pool=timing_alpha_pool,
            specialization_pool=specialization_pool,
            consistency_pool=consistency_pool,
            discipline_pool=discipline_pool,
            brier_baseline_pool=brier_baseline_pool,
        )

    async def _write_gate_rejected_event(
        self,
        *,
        event_repo: TraderEventRepository,
        wallet: str,
        current: object,
        gates: object,
    ) -> None:
        """Persiste une row ``trader_events.event_type='gate_rejected'`` (M12 §4.3).

        ``gates.failed_gate`` contient le :class:`GateResult` rejeté (name,
        observed_value, threshold, reason). Event_metadata structuré pour
        faciliter le drill-down dashboard.
        """
        failed = getattr(gates, "failed_gate", None)
        if failed is None:
            return  # défense en profondeur
        current_status = _current_status_value(current)
        await event_repo.insert(
            TraderEventDTO(
                wallet_address=wallet,
                event_type="gate_rejected",
                from_status=current_status,
                to_status=current_status,
                score_at_event=None,
                scoring_version="v2",
                reason=str(failed.reason)[:128],
                event_metadata={
                    "gate": str(failed.gate_name),
                    "value": failed.observed_value,
                    "threshold": failed.threshold,
                },
            ),
        )
        log.info(
            "trader_gate_rejected",
            wallet=wallet,
            gate=str(failed.gate_name),
            value=failed.observed_value,
            threshold=failed.threshold,
        )

    async def _push_promoted_alert(self, wallet: str, score: float) -> None:
        self._push_alert(
            Alert(
                level="INFO",
                event="trader_promoted",
                body=f"Wallet {wallet} promoted to active (score {score:.2f}).",
                cooldown_key=f"trader_promoted_{wallet}",
            ),
        )

    async def _push_demoted_alert(self, wallet: str, score: float) -> None:
        self._push_alert(
            Alert(
                level="WARNING",
                event="trader_demoted",
                body=f"Wallet {wallet} demoted (score {score:.2f} below threshold).",
                cooldown_key=f"trader_demoted_{wallet}",
            ),
        )

    def _push_alert(self, alert: Alert) -> None:
        if self._alerts is None:
            return
        try:
            self._alerts.put_nowait(alert)
        except asyncio.QueueFull:
            log.warning("alerts_queue_full_dropped", event=alert.event)


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> bool:
    """Attend `seconds` ou jusqu'à `stop_event.set()`. Retourne True si stop reçu."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


def _current_status_value(trader: object) -> str | None:
    """Extrait ``status`` d'un :class:`TargetTrader` ou None si absent.

    Helper tolérant (duck-typed) pour les branches M12 gate_rejected — évite
    d'introduire un import cyclique vers :class:`TargetTrader` et reste
    robuste aux shapes de test stubs.
    """
    if trader is None:
        return None
    status = getattr(trader, "status", None)
    if isinstance(status, str):
        return status
    return None
