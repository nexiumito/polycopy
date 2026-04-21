"""EvictionScheduler — orchestre l'eviction compétitive M5_bis.

Appelé par :class:`~polycopy.discovery.orchestrator.DiscoveryOrchestrator`
en aval du :class:`~polycopy.discovery.decision_engine.DecisionEngine`
M5 (hook Phase C) : une fois les décisions M5 de base appliquées, ce
scheduler ajoute la couche compétition — T3/T5/T7 (cascade) + T6/T8
(sell_only → ?) + T10/T11/T12 (blacklist reconciliation).

**Invariant Phase B** : aucune transition n'est appliquée en DB si
``EVICTION_ENABLED=false``. Le scheduler n'est **pas instancié** dans
ce cas par le DiscoveryOrchestrator (cf. spec §13).

Surface publique :

- :meth:`run_cycle` — appelé 1× par cycle Discovery, retourne la liste
  des :class:`EvictionDecision` appliquées.
- :meth:`reconcile_blacklist` — appelé 1× au boot + 1× à chaque cycle
  (idempotent).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

import structlog
from sqlalchemy import func, select

from polycopy.discovery.eviction.cascade_planner import (
    CascadePlanner,
    TraderSnapshot,
)
from polycopy.discovery.eviction.dtos import EvictionDecision, HysteresisDirection
from polycopy.discovery.eviction.hysteresis_tracker import HysteresisTracker
from polycopy.discovery.eviction.state_machine import (
    StateMachineInputs,
    classify_sell_only_transitions,
    reconcile_blacklist_decisions,
)
from polycopy.monitoring.dtos import Alert
from polycopy.storage.models import MyPosition

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from polycopy.config import Settings
    from polycopy.storage.repositories import TargetTraderRepository

log = structlog.get_logger(__name__)


class EvictionScheduler:
    """Orchestre l'eviction : fetch snapshot → planner + state_machine → apply.

    Stateful (porte un :class:`HysteresisTracker` in-memory). Instancié
    une seule fois par run par :class:`DiscoveryOrchestrator` quand
    ``EVICTION_ENABLED=true``.
    """

    def __init__(
        self,
        target_repo: TargetTraderRepository,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        alerts_queue: asyncio.Queue[Alert] | None = None,
        hysteresis_tracker: HysteresisTracker | None = None,
    ) -> None:
        self._target_repo = target_repo
        self._sf = session_factory
        self._settings = settings
        self._alerts = alerts_queue
        self._hysteresis = hysteresis_tracker or HysteresisTracker()
        self._planner = CascadePlanner(
            score_margin=settings.eviction_score_margin,
            max_sell_only_wallets=settings.max_sell_only_wallets,
        )

    @property
    def hysteresis(self) -> HysteresisTracker:
        """Expose le tracker pour les tests (lecture seule en prod)."""
        return self._hysteresis

    async def run_cycle(
        self,
        scores_by_wallet: dict[str, float],
    ) -> list[EvictionDecision]:
        """Exécute un cycle complet d'eviction. Retourne les décisions appliquées.

        Séquence :
        1. Fetch snapshot TargetTrader + open_positions_count par sell_only.
        2. Classify T6/T8 via EvictionStateMachine.
        3. Appeler CascadePlanner pour T3/T5/T7 (1 swap max).
        4. Appliquer les décisions en DB (transition_status + set_eviction_state).
        5. Push alerts Telegram pour chaque décision actionnable.
        """
        cfg = self._settings
        snapshots = await self._build_snapshots()
        blacklist_lc = {w.lower() for w in cfg.blacklisted_wallets}

        inputs = StateMachineInputs(
            traders=snapshots,
            scores=scores_by_wallet,
            score_margin=cfg.eviction_score_margin,
            hysteresis_cycles=cfg.eviction_hysteresis_cycles,
        )
        decisions: list[EvictionDecision] = []

        # Étape 1 : classifier les sell_only (T6 / T8).
        sell_only_decisions = classify_sell_only_transitions(
            inputs, self._hysteresis, blacklist=blacklist_lc,
        )
        decisions.extend(sell_only_decisions)

        # Étape 2 : plan cascade (T3 ou T7 + T5 simultané).
        cascade_decisions = self._classify_cascade(snapshots, scores_by_wallet)
        decisions.extend(cascade_decisions)

        # Étape 3 : appliquer en DB + push alerts.
        for decision in decisions:
            await self._apply_decision(decision)
            await self._push_alert(decision)

        if decisions:
            log.info(
                "eviction_cycle_completed",
                decisions_count=len(decisions),
                transitions=[d.transition for d in decisions],
            )
        return decisions

    async def reconcile_blacklist(self) -> list[EvictionDecision]:
        """Aligne ``target_traders.status`` avec ``BLACKLISTED_WALLETS`` env.

        Appelé au boot (dans :class:`DiscoveryOrchestrator` Phase C)
        puis à chaque cycle (idempotent). Le pool ``target_wallets``
        env sert à décider si un wallet retiré de la blacklist retourne
        en ``shadow`` ou ``pinned``.
        """
        cfg = self._settings
        snapshots = await self._build_snapshots()
        decisions = reconcile_blacklist_decisions(
            snapshots,
            blacklist=set(cfg.blacklisted_wallets),
            target_wallets=set(cfg.target_wallets),
        )
        for decision in decisions:
            await self._apply_decision(decision)
            await self._push_alert(decision)
        if decisions:
            log.info(
                "blacklist_reconcile_applied",
                count=len(decisions),
                transitions=[d.transition for d in decisions],
            )
        return decisions

    async def _build_snapshots(self) -> list[TraderSnapshot]:
        """Fetch TargetTrader + open_positions_count (par sell_only)."""
        traders = await self._target_repo.list_all()
        # Count open positions only for sell_only wallets (optimization — on
        # n'a pas besoin du count pour active/shadow/pinned/blacklisted).
        # Les positions ne sont pas attachées à un wallet (M3/M8) — elles sont
        # attachées à un condition_id/asset_id indépendamment du wallet source.
        # Pour M5_bis Phase B le count "positions ouvertes d'un wallet" est
        # approximé par "total positions ouvertes du bot" (le bot ne trade
        # qu'un wallet à la fois par condition_id). Affinement possible en
        # Phase C via un mapping condition_id → source wallet via
        # detected_trades.
        open_positions_total = 0
        async with self._sf() as session:
            stmt = select(func.count(MyPosition.id)).where(MyPosition.closed_at.is_(None))
            open_positions_total = int((await session.execute(stmt)).scalar_one())

        snapshots = []
        for t in traders:
            open_count = open_positions_total if t.status == "sell_only" else 0
            snapshots.append(
                TraderSnapshot(
                    wallet_address=t.wallet_address,
                    status=t.status,
                    score=t.score,
                    pinned=t.pinned,
                    eviction_triggering_wallet=t.eviction_triggering_wallet,
                    open_positions_count=open_count,
                ),
            )
        return snapshots

    def _classify_cascade(
        self,
        snapshots: list[TraderSnapshot],
        scores: dict[str, float],
    ) -> list[EvictionDecision]:
        """Applique le planner + projette en EvictionDecision (T3/T5/T7).

        Gère la tick d'hystérésis : le candidat top doit avoir armé
        l'hystérésis via :class:`HysteresisTracker` pendant
        ``EVICTION_HYSTERESIS_CYCLES`` cycles avant de déclencher.
        """
        # Surcharger les scores dans les snapshots avec les scores frais
        # du cycle courant (le plan décide sur ces scores, pas sur les
        # target_traders.score qui sont les scores du cycle précédent).
        refreshed = [
            TraderSnapshot(
                wallet_address=s.wallet_address,
                status=s.status,
                score=scores.get(s.wallet_address.lower(), s.score),
                pinned=s.pinned,
                eviction_triggering_wallet=s.eviction_triggering_wallet,
                open_positions_count=s.open_positions_count,
            )
            for s in snapshots
        ]
        plan = self._planner.plan(refreshed)
        if plan.deferred_sell_only_cap:
            log.warning(
                "eviction_deferred_sell_only_cap",
                max_sell_only=self._settings.max_sell_only_wallets,
            )
            return []
        if plan.promote_candidate is None or plan.demote_worst is None:
            # Reset hystérésis pour tous les wallets qui étaient en
            # direction="eviction"/"rebound" — la condition n'est plus
            # remplie ce cycle.
            for tracked_wallet, state in list(self._hysteresis.snapshot().items()):
                if state.direction in ("eviction", "rebound"):
                    self._hysteresis.reset(tracked_wallet)
            return []

        candidate = plan.promote_candidate
        direction: HysteresisDirection = (
            "rebound" if candidate.from_status == "sell_only" else "eviction"
        )
        cycles = self._hysteresis.tick(
            candidate.wallet_address,
            direction=direction,
            target_wallet=plan.demote_worst,
            current_delta=candidate.delta_vs_worst_active,
        )
        decisions: list[EvictionDecision] = []
        if cycles < self._settings.eviction_hysteresis_cycles:
            log.debug(
                "eviction_hysteresis_pending",
                wallet=candidate.wallet_address,
                cycles=cycles,
                required=self._settings.eviction_hysteresis_cycles,
                delta=round(candidate.delta_vs_worst_active, 4),
            )
            return decisions

        # Hystérésis satisfaite : déclencher la cascade.
        transition = "promote_via_rebound" if direction == "rebound" else "promote_via_eviction"
        decisions.append(
            EvictionDecision(
                wallet_address=candidate.wallet_address,
                transition=transition,  # type: ignore[arg-type]
                from_status=candidate.from_status,
                to_status="active",
                score_at_event=candidate.score,
                delta_vs_worst_active=candidate.delta_vs_worst_active,
                triggering_wallet=plan.demote_worst,
                cycles_observed=cycles,
                reason_code="delta_above_margin",
            ),
        )
        decisions.append(
            EvictionDecision(
                wallet_address=plan.demote_worst,
                transition="demote_to_sell_only",
                from_status="active",
                to_status="sell_only",
                score_at_event=scores.get(plan.demote_worst),
                delta_vs_worst_active=-candidate.delta_vs_worst_active,
                triggering_wallet=candidate.wallet_address,
                cycles_observed=cycles,
                reason_code="cascaded_by_eviction",
            ),
        )
        self._hysteresis.reset(candidate.wallet_address)
        # Les candidats deferred génèrent un audit event (pas d'alerte Telegram).
        for deferred in plan.deferred_one_per_cycle:
            decisions.append(
                EvictionDecision(
                    wallet_address=deferred.wallet_address,
                    transition="defer_one_per_cycle",
                    from_status=deferred.from_status,
                    to_status=deferred.from_status,
                    score_at_event=deferred.score,
                    delta_vs_worst_active=deferred.delta_vs_worst_active,
                    triggering_wallet=plan.demote_worst,
                    reason_code="one_swap_per_cycle",
                ),
            )
        return decisions

    async def _apply_decision(self, decision: EvictionDecision) -> None:
        """Applique la transition en DB. No-op pour les defer_* (audit only).

        Règle : toute sortie de ``sell_only`` clear les colonnes eviction
        (``entered_at=None``, ``triggering_wallet=None``) ; l'entrée en
        ``sell_only`` les pose avec le candidat évinceur. Blacklist passe
        par la méthode unsafe (override pinned safeguard).
        """
        if decision.transition.startswith("defer_"):
            return
        wallet = decision.wallet_address
        now = datetime.now(tz=UTC)

        if decision.transition == "demote_to_sell_only":
            await self._target_repo.transition_status(
                wallet, new_status="sell_only", reset_hysteresis=True,
            )
            await self._target_repo.set_eviction_state(
                wallet, entered_at=now, triggering_wallet=decision.triggering_wallet,
            )
            return

        if decision.transition in (
            "promote_via_eviction",
            "promote_via_rebound",
            "abort_to_active",
            "complete_to_shadow",
        ):
            target_status: Literal["shadow", "active"] = (
                "shadow" if decision.transition == "complete_to_shadow" else "active"
            )
            await self._target_repo.transition_status(
                wallet, new_status=target_status, reset_hysteresis=True,
            )
            await self._target_repo.set_eviction_state(
                wallet, entered_at=None, triggering_wallet=None,
            )
            return

        if decision.transition == "blacklist":
            await self._target_repo.transition_status_unsafe(
                wallet, new_status="blacklisted",
            )
            await self._target_repo.set_eviction_state(
                wallet, entered_at=None, triggering_wallet=None,
            )
            return

        if decision.transition == "unblacklist":
            restore_status = decision.to_status
            if restore_status not in ("shadow", "pinned"):
                log.warning(
                    "unblacklist_invalid_to_status",
                    wallet=wallet,
                    to_status=restore_status,
                )
                return
            await self._target_repo.transition_status_unsafe(
                wallet,
                new_status=cast("Literal['shadow', 'pinned']", restore_status),
            )

    async def _push_alert(self, decision: EvictionDecision) -> None:
        """Push l'alerte Telegram correspondante (si queue présente).

        Les transitions defer_* ne génèrent pas d'alerte (trop bavard,
        conforme §11). Les events Telegram sont routés par
        ``alert_renderer`` vers le template ``<event>.md.j2`` Phase D.
        """
        if self._alerts is None:
            return
        if decision.transition.startswith("defer_") or decision.transition == "demote_to_sell_only":
            # demote_to_sell_only est toujours cascadé par un promote_* ; on
            # émet une seule alerte "eviction_started" pour le couple (cf.
            # spec §11 : 4 alertes + 1 blacklist, pas 5 pour une cascade).
            return
        event_map: dict[str, tuple[str, Literal["INFO", "WARNING"]]] = {
            "promote_via_eviction": ("trader_eviction_started", "INFO"),
            "abort_to_active": ("trader_eviction_aborted", "INFO"),
            "promote_via_rebound": ("trader_eviction_completed_to_active_via_rebound", "INFO"),
            "complete_to_shadow": ("trader_eviction_completed_to_shadow", "INFO"),
            "blacklist": ("trader_blacklisted", "WARNING"),
            "unblacklist": ("trader_blacklist_removed", "INFO"),
        }
        entry = event_map.get(decision.transition)
        if entry is None:
            return
        event_name, level = entry
        body = _short_body_for(decision)
        try:
            self._alerts.put_nowait(
                Alert(
                    level=level,
                    event=event_name,
                    body=body,
                    cooldown_key=event_name,
                ),
            )
        except asyncio.QueueFull:
            log.warning("alerts_queue_full_dropped", event=event_name)


def _short(wallet: str) -> str:
    if len(wallet) < 10:
        return wallet
    return f"{wallet[:6]}…{wallet[-4:]}"


def _short_body_for(decision: EvictionDecision) -> str:
    """Formatte un body Markdown-safe pour l'alerte Telegram."""
    wallet = _short(decision.wallet_address)
    trig = _short(decision.triggering_wallet) if decision.triggering_wallet else "—"
    score = f"{decision.score_at_event:.2f}" if decision.score_at_event is not None else "n/a"
    delta = (
        f"{decision.delta_vs_worst_active:+.2f}"
        if decision.delta_vs_worst_active is not None
        else "n/a"
    )
    cycles = decision.cycles_observed or 0
    if decision.transition == "promote_via_eviction":
        return (
            f"Candidat : {wallet} (score {score}, {decision.from_status})\n"
            f"Évincé   : {trig} (active → sell_only)\n"
            f"Delta    : {delta} sur {cycles} cycles"
        )
    if decision.transition == "abort_to_active":
        return (
            f"Wallet {wallet} revient en active (abort eviction).\n"
            f"Candidat triggering : {trig}\n"
            f"Delta retombé sous margin pendant {cycles} cycles."
        )
    if decision.transition == "promote_via_rebound":
        return (
            f"Rebond : {wallet} (sell_only, score {score}) → active\n"
            f"Nouveau évincé : {trig}\n"
            f"Delta {delta} sur {cycles} cycles."
        )
    if decision.transition == "complete_to_shadow":
        return (
            f"{wallet} (sell_only) → shadow (toutes positions fermées).\n"
            f"Score conservé : {score}. Re-observation possible."
        )
    if decision.transition == "blacklist":
        return (
            f"{wallet} → blacklisted (ajouté manuellement à BLACKLISTED_WALLETS).\n"
            f"Status terminal jusqu'à retrait de l'env var."
        )
    if decision.transition == "unblacklist":
        return (
            f"{wallet} retiré de BLACKLISTED_WALLETS → {decision.to_status}.\n"
            f"Score reset pour re-scoring."
        )
    return f"{wallet}: {decision.transition} (reason={decision.reason_code})"


# --- Re-exports pour le package __init__ --------------------------------------

__all__ = ["EvictionScheduler"]


