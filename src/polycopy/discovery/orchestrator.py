"""Orchestrator du cycle M5 (découverte + scoring + décisions).

Pattern analogue à ``PnlSnapshotWriter`` (M4) et ``DashboardOrchestrator``
(M4.5) : une seule coroutine `run_forever(stop_event)` schedulée dans le
`asyncio.TaskGroup` de ``__main__``, sleep interruptible via ``stop_event``.

Zéro instanciation si ``settings.discovery_enabled=False`` — garde dans
``__main__`` (cf. spec §2.6).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx
import structlog

from polycopy.discovery.candidate_pool import CandidatePool
from polycopy.discovery.data_api_client import DiscoveryDataApiClient
from polycopy.discovery.decision_engine import DecisionEngine
from polycopy.discovery.dtos import DiscoveryDecision, ScoringResult
from polycopy.discovery.goldsky_client import GoldskyClient
from polycopy.discovery.metrics_collector import MetricsCollector
from polycopy.discovery.scoring import compute_score
from polycopy.monitoring.dtos import Alert
from polycopy.storage.dtos import TraderEventDTO, TraderScoreDTO
from polycopy.storage.repositories import (
    TargetTraderRepository,
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
    "demote_paused": "demoted_paused",
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
            max_active_traders=cfg.max_active_traders,
            trader_shadow_days=cfg.trader_shadow_days,
        )
        if cfg.trader_shadow_days == 0 and cfg.discovery_shadow_bypass:
            log.warning(
                "discovery_shadow_bypass_enabled",
                reason="auto_promote_immediate",
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
            decision_engine = DecisionEngine(target_repo, cfg, self._alerts)

            log.info("discovery_started")

            while not stop_event.is_set():
                try:
                    await self._run_one_cycle(
                        candidate_pool,
                        metrics_collector,
                        score_repo,
                        event_repo,
                        target_repo,
                        decision_engine,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("discovery_cycle_failed")
                    self._push_alert(
                        Alert(
                            level="ERROR",
                            event="discovery_cycle_failed",
                            body="Discovery cycle raised an exception, see structured logs.",
                            cooldown_key="discovery_cycle_failed",
                        ),
                    )
                    # Retry rapide après un crash (1 min) avant le sleep normal.
                    if await _sleep_or_stop(stop_event, 60.0):
                        break
                    continue
                if await _sleep_or_stop(stop_event, float(cfg.discovery_interval_seconds)):
                    break
        log.info("discovery_stopped")

    async def _run_one_cycle(
        self,
        candidate_pool: CandidatePool,
        metrics_collector: MetricsCollector,
        score_repo: TraderScoreRepository,
        event_repo: TraderEventRepository,
        target_repo: TargetTraderRepository,
        decision_engine: DecisionEngine,
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
        active_count = active_count_start

        # 5. Scoring séquentiel par wallet (le client HTTP a son propre semaphore).
        for wallet in to_score:
            try:
                metrics = await metrics_collector.collect(wallet)
            except Exception:
                log.exception("metrics_collect_failed", wallet=wallet)
                continue

            score_value, low_conf = compute_score(metrics, settings=self._settings)
            scoring = ScoringResult(
                wallet_address=wallet,
                score=score_value,
                scoring_version=self._settings.scoring_version,
                low_confidence=low_conf,
                metrics=metrics,
                cycle_at=cycle_at,
            )

            # Persist score (seulement si on a un trader en DB — sinon
            # `discovered_shadow` va le créer ci-dessous puis on skip le score_repo
            # pour ce cycle ; le prochain cycle l'aura).
            current = existing_by_wallet.get(wallet)
            if current is not None:
                await score_repo.insert(
                    TraderScoreDTO(
                        target_trader_id=current.id,
                        wallet_address=wallet,
                        score=score_value,
                        scoring_version=self._settings.scoring_version,
                        low_confidence=low_conf,
                        metrics_snapshot=metrics.model_dump(mode="json"),
                    ),
                )
                await target_repo.update_score(
                    wallet,
                    score=score_value,
                    scoring_version=self._settings.scoring_version,
                    scored_at=cycle_at,
                )

            decision = await decision_engine.decide(
                scoring,
                current,
                active_count=active_count,
            )
            await self._persist_event(event_repo, decision, scoring, current, cycle_at)
            # Compteurs
            if decision.decision == "promote_active":
                promotions += 1
                active_count += 1
                await self._push_promoted_alert(wallet, score_value)
            elif decision.decision == "demote_paused":
                demotions += 1
                active_count -= 1
                await self._push_demoted_alert(wallet, score_value)
            elif decision.decision == "discovered_shadow":
                discovered += 1
            elif decision.decision in ("keep", "revived_shadow"):
                kept += 1
            elif decision.decision in ("skip_blacklist", "skip_cap"):
                skipped += 1

            log.debug(
                "score_computed",
                wallet=wallet,
                score=round(score_value, 4),
                low_confidence=low_conf,
                decision=decision.decision,
            )

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
            active_count=active_count,
            duration_ms=duration_ms,
            scoring_version=self._settings.scoring_version,
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
