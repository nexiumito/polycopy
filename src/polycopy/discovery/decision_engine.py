"""Decision engine M5.

Règles déterministes (spec §7.5) sur un wallet × son score × son statut courant :

- wallet ∈ BLACKLISTED_WALLETS → skip_blacklist, ZÉRO write DB + ZÉRO alert.
- wallet absent + score élevé + cap OK → discovered_shadow (ou promote direct
  si TRADER_SHADOW_DAYS=0 ET DISCOVERY_SHADOW_BYPASS=true).
- wallet pinned → TOUJOURS keep (jamais demote).
- wallet shadow + days écoulés + score OK + cap OK → promote_active.
- wallet active + score < demote + K cycles → demote_paused (hystérésis).
- wallet paused + score remonté → revived_shadow (réinjection observation).

Toutes les décisions sont retournées en `DiscoveryDecision` — le caller
(orchestrator) est responsable de l'insert `trader_events` + émission alerts.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from polycopy.discovery.dtos import DiscoveryDecision, ScoringResult
from polycopy.monitoring.dtos import Alert
from polycopy.storage.models import TargetTrader
from polycopy.storage.repositories import TargetTraderRepository

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)


class DecisionEngine:
    """Stateless (par cycle) : transform un `ScoringResult` en `DiscoveryDecision`."""

    def __init__(
        self,
        target_repo: TargetTraderRepository,
        settings: Settings,
        alerts_queue: asyncio.Queue[Alert] | None = None,
    ) -> None:
        self._target_repo = target_repo
        self._settings = settings
        self._alerts = alerts_queue

    async def decide(
        self,
        scoring: ScoringResult,
        current: TargetTrader | None,
        *,
        active_count: int,
    ) -> DiscoveryDecision:
        """Applique les règles. ``active_count`` = nb `status='active'` live.

        Le caller doit passer un compteur frais ; l'engine ne promeut que si
        ``active_count < MAX_ACTIVE_TRADERS``. En cas de `skip_cap`, l'alerte
        `discovery_cap_reached` est poussée ici.
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
            # Default : insert shadow
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
            return await self._decide_active(current, score, version)

        if current.status == "paused":
            return await self._decide_paused(current, score, version, active_count)

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
        cfg = self._settings
        wallet = current.wallet_address
        if score >= cfg.scoring_demotion_threshold:
            # Score acceptable : reset hystérésis
            if current.consecutive_low_score_cycles > 0:
                await self._target_repo.reset_low_score(wallet)
            return DiscoveryDecision(
                wallet_address=wallet,
                decision="keep",
                from_status="active",
                to_status="active",
                score_at_event=score,
                scoring_version=version,
                reason=f"score {score:.2f} >= demotion {cfg.scoring_demotion_threshold:.2f}",
            )
        # Sous le seuil : incrément hystérésis
        new_count = await self._target_repo.increment_low_score(wallet)
        if new_count >= cfg.scoring_demotion_hysteresis_cycles:
            await self._target_repo.transition_status(
                wallet,
                new_status="paused",
                reset_hysteresis=True,
            )
            log.warning(
                "trader_demoted",
                wallet=wallet,
                score=score,
                cycles_under_threshold=new_count,
            )
            return DiscoveryDecision(
                wallet_address=wallet,
                decision="demote_paused",
                from_status="active",
                to_status="paused",
                score_at_event=score,
                scoring_version=version,
                reason=f"{new_count} cycles under {cfg.scoring_demotion_threshold:.2f}",
                event_metadata={"cycles_under_threshold": new_count},
            )
        return DiscoveryDecision(
            wallet_address=wallet,
            decision="keep",
            from_status="active",
            to_status="active",
            score_at_event=score,
            scoring_version=version,
            reason=(
                f"under threshold {new_count}/{cfg.scoring_demotion_hysteresis_cycles} "
                f"(score {score:.2f})"
            ),
            event_metadata={"cycles_under_threshold": new_count},
        )

    async def _decide_paused(
        self,
        current: TargetTrader,
        score: float,
        version: str,
        active_count: int,
    ) -> DiscoveryDecision:
        cfg = self._settings
        wallet = current.wallet_address
        # Revival : si score remonte au-dessus du seuil de promotion, re-inject
        # en shadow (ré-observation, pas de promotion immédiate).
        if score >= cfg.scoring_promotion_threshold:
            await self._target_repo.transition_status(
                wallet,
                new_status="shadow",
                reset_hysteresis=True,
            )
            log.info("trader_revived_shadow", wallet=wallet, score=score)
            return DiscoveryDecision(
                wallet_address=wallet,
                decision="revived_shadow",
                from_status="paused",
                to_status="shadow",
                score_at_event=score,
                scoring_version=version,
                reason=f"paused → shadow on score {score:.2f}",
            )
        del active_count  # non utilisé dans la branche paused
        return DiscoveryDecision(
            wallet_address=wallet,
            decision="keep",
            from_status="paused",
            to_status="paused",
            score_at_event=score,
            scoring_version=version,
            reason=f"paused, score {score:.2f} < promotion",
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
