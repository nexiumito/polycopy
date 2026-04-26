"""Decision engine M5 + M5_bis.

Règles déterministes (spec §7.5) sur un wallet × son score × son statut courant :

- wallet ∈ BLACKLISTED_WALLETS → skip_blacklist, ZÉRO write DB + ZÉRO alert.
- wallet absent + score élevé + cap OK → discovered_shadow (ou promote direct
  si TRADER_SHADOW_DAYS=0 ET DISCOVERY_SHADOW_BYPASS=true).
- wallet pinned → TOUJOURS keep (jamais demote).
- wallet shadow + days écoulés + score OK + cap OK → promote_active.
- wallet active + score < demote + K cycles → **demote_shadow** (M5_bis Phase C :
  ex-``demote_paused`` fusionné avec shadow + flag UX ``previously_demoted_at``).
- wallet sell_only → keep (transitions T6/T7/T8 pilotées par EvictionScheduler).
- wallet blacklisted → keep (transitions T11/T12 pilotées par reconcile_blacklist).

Toutes les décisions sont retournées en `DiscoveryDecision` — le caller
(orchestrator) est responsable de l'insert `trader_events` + émission alerts.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from polycopy.discovery.dtos import DiscoveryDecision, ScoringResult
from polycopy.monitoring.dtos import Alert
from polycopy.storage.models import TargetTrader
from polycopy.storage.repositories import TargetTraderRepository

if TYPE_CHECKING:
    from polycopy.config import Settings
    from polycopy.storage.repositories import MyPositionRepository

log = structlog.get_logger(__name__)

# M15 MB.8 — fenêtre observation auto-blacklist (cohérent MB.1 internal_pnl).
_AUTO_BLACKLIST_WINDOW_DAYS: int = 30
# M15 MB.8 — seuil dur win_rate (séparé du Pydantic Settings pour rester
# pure dans le helper).
_AUTO_BLACKLIST_WIN_RATE_FLOOR: float = 0.25


class DecisionEngine:
    """Stateless (par cycle) : transform un `ScoringResult` en `DiscoveryDecision`."""

    def __init__(
        self,
        target_repo: TargetTraderRepository,
        settings: Settings,
        alerts_queue: asyncio.Queue[Alert] | None = None,
        my_positions_repo: MyPositionRepository | None = None,
    ) -> None:
        self._target_repo = target_repo
        self._settings = settings
        self._alerts = alerts_queue
        # M15 MB.8 : repo MyPosition pour l'auto-blacklist (consomme
        # sum_realized_pnl_by_source_wallet + count_wins_losses_*).
        # Optional pour rétrocompat tests M5/M5_bis qui n'injectent pas.
        self._my_positions_repo = my_positions_repo

    async def decide(
        self,
        scoring: ScoringResult,
        current: TargetTrader | None,
        *,
        active_count: int,
        trade_count_90d: int | None = None,
        days_active: int | None = None,
    ) -> DiscoveryDecision:
        """Applique les règles. ``active_count`` = nb `status='active'` live.

        Le caller doit passer un compteur frais ; l'engine ne promeut que si
        ``active_count < MAX_ACTIVE_TRADERS``. En cas de `skip_cap`, l'alerte
        `discovery_cap_reached` est poussée ici.

        M15 MB.6 : ``trade_count_90d`` + ``days_active`` (optionnels — issus
        du scoring v2.1+) permettent au DecisionEngine de :

        1. Détecter un wallet candidat en **probation** (10 ≤ trade_count
           < 50 ET days_active ≥ 7) à l'insertion shadow → flag
           ``is_probation=True``.
        2. Auto-release un wallet ACTIVE déjà en probation quand
           ``trade_count_90d ≥ 50 ET days_active ≥ 30``.

        Si ``None`` (M5 callers, tests legacy) → aucune logique probation
        (rétrocompat strict M5/M14 préservée).
        """
        wallet = scoring.wallet_address.lower()
        score = scoring.score
        version = scoring.scoring_version
        cfg = self._settings

        blacklist = {w.lower() for w in cfg.blacklisted_wallets}
        if wallet in blacklist:
            return DiscoveryDecision(
                wallet_address=wallet,
                decision="skip_blacklist",
                from_status=_current_status(current),
                to_status=_current_status(current) or "absent",  # pas de write
                score_at_event=score,
                scoring_version=version,
                reason="blacklisted",
            )

        # --- Wallet absent : découverte ou promotion directe --------------
        if current is None:
            if score < cfg.scoring_promotion_threshold:
                return DiscoveryDecision(
                    wallet_address=wallet,
                    decision="keep",
                    from_status="absent",
                    to_status="absent",
                    score_at_event=score,
                    scoring_version=version,
                    reason=f"score {score:.2f} < promotion {cfg.scoring_promotion_threshold:.2f}",
                )
            # Score suffisant : vérifier cap
            if active_count >= cfg.max_active_traders:
                await self._push_cap_alert(wallet, score)
                return DiscoveryDecision(
                    wallet_address=wallet,
                    decision="skip_cap",
                    from_status="absent",
                    to_status="absent",
                    score_at_event=score,
                    scoring_version=version,
                    reason=f"cap={cfg.max_active_traders} reached",
                )
            # Bypass shadow ? (uniquement shadow_days=0 AND bypass=True)
            if cfg.trader_shadow_days == 0 and cfg.discovery_shadow_bypass:
                new_trader = await self._target_repo.insert_shadow(wallet)
                await self._target_repo.transition_status(
                    wallet,
                    new_status="active",
                    reset_hysteresis=True,
                )
                log.info(
                    "trader_promoted",
                    wallet=wallet,
                    score=score,
                    from_status="absent",
                    to_status="active",
                    bypass_shadow=True,
                )
                del new_trader  # juste pour clarté
                return DiscoveryDecision(
                    wallet_address=wallet,
                    decision="promote_active",
                    from_status="absent",
                    to_status="active",
                    score_at_event=score,
                    scoring_version=version,
                    reason=f"shadow_bypass + score {score:.2f} >= threshold",
                    event_metadata={"bypass_shadow": True},
                )
            # M15 MB.6 : path probation pour wallets [10, 50) trades.
            #
            # Si l'orchestrator nous a passé `trade_count_90d` + `days_active`
            # ET que le wallet est dans la fenêtre probation MAIS sous le
            # gate full M14 (>=50 trades / >=30 jours), on l'insère shadow
            # avec `is_probation=True`. Sinon path standard.
            #
            # Les autres gates (cash_pnl, not_blacklisted, not_wash_cluster,
            # not_arbitrage_bot, zombie_ratio) restent stricts — appliqués
            # par `gates.check_all_gates` côté orchestrator avant scoring.
            # MB.6 ne relax que `trade_count` (≥10 vs ≥50) et `days_active`
            # (≥7 vs ≥30). Cf. spec §5.6 + §8.5.
            is_probation_candidate = self._is_probation_candidate(
                trade_count_90d=trade_count_90d,
                days_active=days_active,
            )
            if is_probation_candidate:
                await self._target_repo.insert_shadow(wallet, is_probation=True)
                log.info(
                    "trader_discovered_probation",
                    wallet=wallet,
                    score=score,
                    trade_count_90d=trade_count_90d,
                    days_active=days_active,
                    to_status="shadow",
                )
                return DiscoveryDecision(
                    wallet_address=wallet,
                    decision="discovered_shadow",
                    from_status="absent",
                    to_status="shadow",
                    score_at_event=score,
                    scoring_version=version,
                    reason=(
                        f"probation: trades={trade_count_90d}, "
                        f"days={days_active}, sized "
                        f"{cfg.probation_size_multiplier}× until full gate"
                    ),
                    event_metadata={
                        "is_probation": True,
                        "trade_count_90d": trade_count_90d,
                        "days_active": days_active,
                    },
                )

            # Default : insert shadow (M5 path standard)
            await self._target_repo.insert_shadow(wallet)
            log.info(
                "trader_discovered",
                wallet=wallet,
                score=score,
                to_status="shadow",
            )
            return DiscoveryDecision(
                wallet_address=wallet,
                decision="discovered_shadow",
                from_status="absent",
                to_status="shadow",
                score_at_event=score,
                scoring_version=version,
                reason=f"score {score:.2f} >= promotion; observing {cfg.trader_shadow_days}d",
            )

        # --- Wallet existant ---------------------------------------------
        # Règle d'or : pinned → jamais touché (safeguard non-négociable).
        if current.pinned:
            return DiscoveryDecision(
                wallet_address=wallet,
                decision="keep",
                from_status="pinned",
                to_status="pinned",
                score_at_event=score,
                scoring_version=version,
                reason="pinned (TARGET_WALLETS seed)",
            )

        if current.status == "shadow":
            return await self._decide_shadow(current, score, version, active_count)

        if current.status == "active":
            # M15 MB.6 : avant le ranking, tenter le release probation si
            # le wallet a passé le gate full (trade_count_90d ≥ 50 ET
            # days_active ≥ 30). Aucun effet si pas en probation ou si
            # metrics non fournis (rétrocompat M5).
            await self._maybe_release_probation(current, trade_count_90d, days_active)
            # M15 MB.8 : avant le ranking, vérifier les seuils auto-blacklist.
            # Si fire → court-circuit + transition `active → blacklisted` +
            # alerte Telegram. Sinon path standard ranking (MB.3).
            auto_bl = await self._maybe_auto_blacklist(current, score, version)
            if auto_bl is not None:
                return auto_bl
            return await self._decide_active(current, score, version)

        if current.status == "sell_only":
            # M5_bis : lifecycle sell_only piloté par EvictionScheduler
            # (T6 abort, T7 rebound, T8 complete). DecisionEngine garde
            # le wallet tel quel pour le cycle courant ; score est
            # quand même écrit dans `trader_scores` par l'orchestrator.
            return DiscoveryDecision(
                wallet_address=wallet,
                decision="keep",
                from_status="sell_only",
                to_status="sell_only",
                score_at_event=score,
                scoring_version=version,
                reason="sell_only lifecycle managed by EvictionScheduler (M5_bis)",
            )

        if current.status == "blacklisted":
            # M5_bis : terminal. Transitions T10/T11/T12 via
            # reconcile_blacklist, pas via DecisionEngine.
            return DiscoveryDecision(
                wallet_address=wallet,
                decision="keep",
                from_status="blacklisted",
                to_status="blacklisted",
                score_at_event=score,
                scoring_version=version,
                reason="blacklisted (managed by reconcile_blacklist)",
            )

        if current.status == "paused":
            # Compat defensive : la migration 0007 M5_bis convertit tous
            # les paused existants en shadow. Si un wallet se retrouve
            # en paused (downgrade DB, seed manuel), on le laisse
            # tranquille ce cycle — le prochain alembic upgrade nettoie.
            log.warning(
                "decision_engine_paused_status_defensive_keep",
                wallet=wallet,
                score=score,
                hint="migration 0007 should have converted paused to shadow",
            )
            return DiscoveryDecision(
                wallet_address=wallet,
                decision="keep",
                from_status="paused",
                to_status="paused",
                score_at_event=score,
                scoring_version=version,
                reason="paused (legacy M5, expected to be cleaned by migration 0007)",
            )

        # Fallback défensif : status inconnu
        log.warning("decision_engine_unknown_status", wallet=wallet, status=current.status)
        return DiscoveryDecision(
            wallet_address=wallet,
            decision="keep",
            from_status=current.status,
            to_status=current.status,
            score_at_event=score,
            scoring_version=version,
            reason=f"unknown status {current.status!r}",
        )

    async def _decide_shadow(
        self,
        current: TargetTrader,
        score: float,
        version: str,
        active_count: int,
    ) -> DiscoveryDecision:
        cfg = self._settings
        # SQLite ne persiste pas le tzinfo sur DateTime(timezone=True) : on
        # ré-injecte UTC si discovered_at revient naïf du driver aiosqlite.
        discovered_at_raw = current.discovered_at or datetime.now(tz=UTC)
        discovered_at = (
            discovered_at_raw
            if discovered_at_raw.tzinfo is not None
            else discovered_at_raw.replace(tzinfo=UTC)
        )
        now = datetime.now(tz=UTC)
        days_observed = (now - discovered_at).total_seconds() / 86400.0
        wallet = current.wallet_address
        if days_observed < cfg.trader_shadow_days or score < cfg.scoring_promotion_threshold:
            return DiscoveryDecision(
                wallet_address=wallet,
                decision="keep",
                from_status="shadow",
                to_status="shadow",
                score_at_event=score,
                scoring_version=version,
                reason=(
                    f"observing ({days_observed:.1f}/{cfg.trader_shadow_days}d, score {score:.2f})"
                ),
                event_metadata={"days_observed": round(days_observed, 2)},
            )
        # Prêt pour promotion — vérifier cap
        if active_count >= cfg.max_active_traders:
            await self._push_cap_alert(wallet, score)
            return DiscoveryDecision(
                wallet_address=wallet,
                decision="skip_cap",
                from_status="shadow",
                to_status="shadow",
                score_at_event=score,
                scoring_version=version,
                reason=f"cap={cfg.max_active_traders} reached",
            )
        await self._target_repo.transition_status(
            wallet,
            new_status="active",
            reset_hysteresis=True,
        )
        log.info("trader_promoted", wallet=wallet, score=score)
        return DiscoveryDecision(
            wallet_address=wallet,
            decision="promote_active",
            from_status="shadow",
            to_status="active",
            score_at_event=score,
            scoring_version=version,
            reason=f"score {score:.2f} >= threshold after {days_observed:.1f}d shadow",
        )

    async def _decide_active(
        self,
        current: TargetTrader,
        score: float,
        version: str,
    ) -> DiscoveryDecision:
        """M15 MB.3 — ranking-based activation.

        Diff vs M5/M14 :

        1. Le critère principal est le **rang** du wallet parmi les
           ``status='active'`` du pool. Hors top-N (``rank >=
           MAX_ACTIVE_TRADERS``) → incrément hystérésis. Pool sub-cap
           (active_count < cap) → personne hors top-N → personne demote
           via ranking (rotation utile uniquement quand on est saturés,
           cf. spec §14.1 D9).
        2. Garde-fou absolu **toujours actif** : si ``score <
           SCORING_ABSOLUTE_HARD_FLOOR=0.30``, on incrémente l'hystérésis
           **et** on demote indépendamment du ranking (cas pathologique :
           pool entièrement < 0.30).
        3. Hystérésis 3 cycles préservée
           (``SCORING_DEMOTION_HYSTERESIS_CYCLES``) — anti-flip-flop.
        4. ``pinned`` jamais ici (filtre amont via ``decide``).

        Cf. spec M15 §5.3 + §9.3.
        """
        cfg = self._settings
        wallet = current.wallet_address.lower()

        # 1. Garde-fou absolu hard floor — ceinture + bretelle.
        if score < cfg.scoring_absolute_hard_floor:
            new_count = await self._target_repo.increment_low_score(wallet)
            if new_count >= cfg.scoring_demotion_hysteresis_cycles:
                return await self._do_demote(
                    current,
                    score,
                    version,
                    new_count,
                    reason=(
                        f"score {score:.2f} < absolute_hard_floor "
                        f"{cfg.scoring_absolute_hard_floor:.2f}"
                    ),
                    ranking_basis="absolute_floor",
                )
            return DiscoveryDecision(
                wallet_address=wallet,
                decision="keep",
                from_status="active",
                to_status="active",
                score_at_event=score,
                scoring_version=version,
                reason=(
                    f"under absolute_hard_floor {new_count}/"
                    f"{cfg.scoring_demotion_hysteresis_cycles} (score {score:.2f})"
                ),
                event_metadata={
                    "cycles_under_threshold": new_count,
                    "ranking_basis": "absolute_floor",
                },
            )

        # 2. Ranking : fetch active scores du pool courant + rank du wallet.
        active_scores = await self._target_repo.list_active_scores()
        sorted_scores = sorted(active_scores, key=lambda r: -r[1])
        wallet_rank = next(
            (i for i, (w, _) in enumerate(sorted_scores) if w == wallet),
            len(sorted_scores),  # wallet absent du snapshot → fall through
        )
        out_of_top_n = wallet_rank >= cfg.max_active_traders

        if not out_of_top_n:
            # Dans le top-N : reset hystérésis + keep.
            if current.consecutive_low_score_cycles > 0:
                await self._target_repo.reset_low_score(wallet)
            return DiscoveryDecision(
                wallet_address=wallet,
                decision="keep",
                from_status="active",
                to_status="active",
                score_at_event=score,
                scoring_version=version,
                reason=(
                    f"rank {wallet_rank + 1}/{len(sorted_scores)} within "
                    f"top-{cfg.max_active_traders} (score {score:.2f})"
                ),
                event_metadata={
                    "wallet_rank": wallet_rank + 1,
                    "ranking_basis": "top_n",
                },
            )

        # 3. Out-of-top-N → incrément hystérésis.
        new_count = await self._target_repo.increment_low_score(wallet)
        if new_count >= cfg.scoring_demotion_hysteresis_cycles:
            return await self._do_demote(
                current,
                score,
                version,
                new_count,
                reason=(
                    f"rank {wallet_rank + 1} > MAX_ACTIVE_TRADERS="
                    f"{cfg.max_active_traders} for {new_count} cycles"
                ),
                ranking_basis="top_n",
                wallet_rank=wallet_rank + 1,
            )
        return DiscoveryDecision(
            wallet_address=wallet,
            decision="keep",
            from_status="active",
            to_status="active",
            score_at_event=score,
            scoring_version=version,
            reason=(
                f"out-of-top-N {new_count}/"
                f"{cfg.scoring_demotion_hysteresis_cycles} (rank "
                f"{wallet_rank + 1}, score {score:.2f})"
            ),
            event_metadata={
                "cycles_out_of_top_n": new_count,
                "wallet_rank": wallet_rank + 1,
                "ranking_basis": "top_n",
            },
        )

    def _is_probation_candidate(
        self,
        *,
        trade_count_90d: int | None,
        days_active: int | None,
    ) -> bool:
        """M15 MB.6 — wallet absent éligible à l'insertion ``is_probation=True``.

        Retourne ``True`` si :

        - ``trade_count_90d`` dans ``[probation_min_trades, probation_full_trades)``
          (default ``[10, 50)``).
        - ``days_active >= probation_min_days`` (default ``≥7``).

        Si ``None`` (M5 caller) → ``False`` (rétrocompat).
        """
        if trade_count_90d is None or days_active is None:
            return False
        cfg = self._settings
        return (
            cfg.probation_min_trades <= trade_count_90d < cfg.probation_full_trades
            and days_active >= cfg.probation_min_days
        )

    async def _maybe_release_probation(
        self,
        current: TargetTrader,
        trade_count_90d: int | None,
        days_active: int | None,
    ) -> None:
        """M15 MB.6 — auto-release ``is_probation=False`` si gate full satisfait.

        Conditions de release strictes :

        - ``current.is_probation == True`` (sinon no-op).
        - ``trade_count_90d ≥ probation_full_trades`` (default 50).
        - ``days_active ≥ probation_full_days`` (default 30).

        Persiste la transition via
        :meth:`TargetTraderRepository.set_probation` + écrit l'event
        ``trader_events.event_type='probation_released'`` dans la prochaine
        version (l'orchestrator écrit l'event via ``_persist_event`` à
        partir de ``DiscoveryDecision`` — ici on ne touche que la DB pour
        garder ``decide()`` idempotent dans son retour).

        Aucun retour : c'est un side-effect pré-decide qui n'altère pas la
        décision retournée. Si le release tire ce cycle, le ``current``
        en mémoire n'est pas re-fetched — peu grave car le path
        downstream (`_decide_active`) ne lit pas `is_probation` directement
        (le sizer côté `PositionSizer` refresh via `WalletPoller`
        resolver).
        """
        if not current.is_probation:
            return
        if trade_count_90d is None or days_active is None:
            return
        cfg = self._settings
        if trade_count_90d >= cfg.probation_full_trades and days_active >= cfg.probation_full_days:
            await self._target_repo.set_probation(current.wallet_address, on=False)
            log.info(
                "trader_probation_released",
                wallet=current.wallet_address,
                trade_count_90d=trade_count_90d,
                days_active=days_active,
            )

    async def _maybe_auto_blacklist(
        self,
        current: TargetTrader,
        score: float,
        version: str,
    ) -> DiscoveryDecision | None:
        """M15 MB.8 — auto-blacklist si critères PnL ou WR violation.

        Critères (OU logique) :

        - ``cumulative_observed_pnl_30d < AUTO_BLACKLIST_PNL_THRESHOLD_USD``
          (default −$5)
        - ``observed_position_count_30d ≥ AUTO_BLACKLIST_MIN_POSITIONS_FOR_WR``
          (default 30) AND ``observed_win_rate < 0.25``

        Idempotence :

        - Wallet déjà ``status='blacklisted'`` (cas race) → no-op.
        - ``MyPositionRepository`` non injecté → no-op (rétrocompat tests M5
          qui ne testent pas l'auto-blacklist).
        - Cooldown alert via ``cooldown_key=f"auto_blacklist_{wallet}"``
          empêche le re-fire dans la fenêtre digest (M7 dispatcher).

        Calcul break-even : ``observed_win_rate = wins / (wins + losses)``
        où break-even (`realized_pnl == 0`) sont **exclus du dénominateur**
        (cf. spec §14.3). ``decided=0`` → ``observed_win_rate=None`` →
        critère WR ne tire pas (neutre, pas mauvais).

        Returns : ``DiscoveryDecision`` si fire, ``None`` sinon.

        Cf. spec M15 §5.8 + §9.8 + §8.4 (réversibilité).
        """
        if self._my_positions_repo is None:
            return None
        cfg = self._settings
        wallet = current.wallet_address.lower()

        # Filtre simulated cohérent MB.1 (live → simulated=False, dry/sim
        # → simulated=True).
        simulated_flag = cfg.execution_mode != "live"
        cutoff = datetime.now(tz=UTC) - timedelta(days=_AUTO_BLACKLIST_WINDOW_DAYS)

        try:
            pnl_sum, count = await self._my_positions_repo.sum_realized_pnl_by_source_wallet(
                wallet_address=wallet,
                since=cutoff,
                simulated=simulated_flag,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "auto_blacklist_repository_error",
                wallet=wallet,
                error=str(exc),
            )
            return None

        if count == 0:
            # Pas de copy data observable → pas de signal.
            return None

        try:
            wins, losses = await self._my_positions_repo.count_wins_losses_by_source_wallet(
                wallet_address=wallet,
                since=cutoff,
                simulated=simulated_flag,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "auto_blacklist_repository_error",
                wallet=wallet,
                error=str(exc),
            )
            return None

        decided = wins + losses
        observed_wr = (wins / decided) if decided > 0 else None

        fires_pnl = pnl_sum < float(cfg.auto_blacklist_pnl_threshold_usd)
        fires_wr = (
            observed_wr is not None
            and decided >= cfg.auto_blacklist_min_positions_for_wr
            and observed_wr < _AUTO_BLACKLIST_WIN_RATE_FLOOR
        )

        if not fires_pnl and not fires_wr:
            return None

        reason_code = "pnl_threshold" if fires_pnl else "win_rate_floor"

        # Transition active → blacklisted via le path unsafe (cohérent
        # M5_bis reconcile_blacklist : transitions T10/T11/T12 piloted
        # par status DB, pas via env update).
        await self._target_repo.transition_status_unsafe(
            wallet_address=wallet,
            new_status="blacklisted",
        )
        log.warning(
            "trader_auto_blacklisted",
            wallet=wallet,
            reason_code=reason_code,
            observed_pnl=round(pnl_sum, 4),
            observed_wr=(round(observed_wr, 4) if observed_wr is not None else None),
            observed_position_count=count,
            score_at_event=score,
        )

        # Alerte Telegram MarkdownV2 (template trader_auto_blacklisted.md.j2).
        # Le body contient les stats user-facing — escape via filter
        # `telegram_md_escape` dans le template.
        if self._alerts is not None:
            wr_str = f"{observed_wr:.1%}" if observed_wr is not None else "N/A"
            body = (
                f"Wallet : `{wallet[:10]}…{wallet[-4:]}`. "
                f"Raison : {reason_code} (PnL observé "
                f"{pnl_sum:.2f} USD sur 30j, win-rate {wr_str} "
                f"sur {decided} positions décidées). "
                f"Status : active → blacklisted."
            )
            try:
                self._alerts.put_nowait(
                    Alert(
                        level="WARNING",
                        event="trader_auto_blacklisted",
                        body=body,
                        cooldown_key=f"auto_blacklist_{wallet}",
                    ),
                )
            except asyncio.QueueFull:
                log.warning(
                    "alerts_queue_full_dropped",
                    event="trader_auto_blacklisted",
                    wallet=wallet,
                )

        return DiscoveryDecision(
            wallet_address=wallet,
            decision="keep",  # M5 DecisionKind ne contient pas auto_blacklist
            from_status="active",
            to_status="blacklisted",
            score_at_event=score,
            scoring_version=version,
            reason=f"auto_blacklist:{reason_code}",
            event_metadata={
                "auto_blacklist": True,
                "reason_code": reason_code,
                "observed_pnl_30d": round(pnl_sum, 4),
                "observed_position_count_30d": count,
                "observed_win_rate_30d": (
                    round(observed_wr, 4) if observed_wr is not None else None
                ),
            },
        )

    async def _do_demote(
        self,
        current: TargetTrader,
        score: float,
        version: str,
        new_count: int,
        *,
        reason: str,
        ranking_basis: str,
        wallet_rank: int | None = None,
    ) -> DiscoveryDecision:
        """Helper privé MB.3 : transition active → shadow + flag UX."""
        wallet = current.wallet_address.lower()
        now = datetime.now(tz=UTC)
        await self._target_repo.transition_status(
            wallet,
            new_status="shadow",
            reset_hysteresis=True,
        )
        # M5_bis Phase C : flag UX pour distinguer un shadow
        # "re-observation après demote" d'un shadow "découvert neuf".
        await self._target_repo.set_previously_demoted_at(wallet, at=now)
        log.warning(
            "trader_demoted",
            wallet=wallet,
            score=score,
            cycles_under_threshold=new_count,
            ranking_basis=ranking_basis,
            wallet_rank=wallet_rank,
            to_status="shadow",
        )
        event_metadata: dict[str, object] = {
            "cycles_under_threshold": new_count,
            "ranking_basis": ranking_basis,
        }
        if wallet_rank is not None:
            event_metadata["wallet_rank"] = wallet_rank
        return DiscoveryDecision(
            wallet_address=wallet,
            decision="demote_shadow",
            from_status="active",
            to_status="shadow",
            score_at_event=score,
            scoring_version=version,
            reason=reason,
            event_metadata=event_metadata,
        )

    async def _push_cap_alert(self, wallet: str, score: float) -> None:
        if self._alerts is None:
            return
        try:
            self._alerts.put_nowait(
                Alert(
                    level="WARNING",
                    event="discovery_cap_reached",
                    body=(
                        f"Wallet {wallet} (score {score:.2f}) skipped: MAX_ACTIVE_TRADERS reached."
                    ),
                    cooldown_key="discovery_cap_reached",
                ),
            )
        except asyncio.QueueFull:
            log.warning("alerts_queue_full_dropped", event="discovery_cap_reached")


def _current_status(trader: TargetTrader | None) -> str | None:
    if trader is None:
        return "absent"
    return trader.status
