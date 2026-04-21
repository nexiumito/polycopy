"""In-memory hysteresis tracker for competitive eviction (M5_bis).

Stocke, par wallet candidat, le compteur de cycles consécutifs où une
condition d'hystérésis a tenu. Le compteur est **reset** si la direction
(eviction/abort/rebound) change OU si la cible (``target_wallet``) change
entre deux cycles — cf. spec §4.5 EC-3 : l'hystérésis est portée par le
candidat, pas par le couple.

**Persistance** : volontairement in-memory (dict process-local). Un
restart reset tous les compteurs, ce qui peut retarder une eviction de
``EVICTION_HYSTERESIS_CYCLES`` cycles supplémentaires (6h × 3 = 18h au
plus dans la config par défaut). Trade-off accepté pour éviter un
nouveau schéma DB + une query par cycle (cf. spec §7.7).

Thread-safety : single-threaded asyncio, donc ``dict`` sans lock suffit
— identique pattern :class:`~polycopy.monitoring.telegram_dispatcher.
AlertDigestWindow`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from polycopy.discovery.eviction.dtos import HysteresisDirection, HysteresisState

log = structlog.get_logger(__name__)


class HysteresisTracker:
    """Compteurs d'hystérésis in-memory par wallet candidat.

    API minimaliste :

    - :meth:`tick` incrémente (ou reset si la direction/cible change) et
      retourne le nombre de cycles consécutifs observés.
    - :meth:`reset` efface explicitement le compteur d'un wallet (utilisé
      quand la condition n'est plus remplie : EC-3 bascule direction, ou
      quand une transition est déclenchée et consomme l'hystérésis).
    - :meth:`count` lecture read-only.
    - :meth:`snapshot` retourne l'état complet (debug + tests).
    """

    def __init__(self) -> None:
        self._states: dict[str, HysteresisState] = {}

    def tick(
        self,
        wallet_address: str,
        *,
        direction: HysteresisDirection,
        target_wallet: str | None,
        current_delta: float,
        metadata: dict[str, str] | None = None,
    ) -> int:
        """Enregistre un tick d'observation de la condition pour ``wallet``.

        Si (direction, target_wallet) change depuis le dernier tick, le
        compteur redémarre à 1 (nouvelle observation distincte). Sinon
        il s'incrémente. Retourne la nouvelle valeur.
        """
        wallet = wallet_address.lower()
        now = datetime.now(tz=UTC)
        state = self._states.get(wallet)
        if state is None or state.direction != direction or state.target_wallet != target_wallet:
            new_state = HysteresisState(
                direction=direction,
                target_wallet=target_wallet,
                cycles_observed=1,
                first_observed_at=now,
                last_delta=current_delta,
                metadata=dict(metadata or {}),
            )
            self._states[wallet] = new_state
            log.debug(
                "hysteresis_started",
                wallet=wallet,
                direction=direction,
                target_wallet=target_wallet,
                delta=round(current_delta, 4),
            )
            return 1
        new_count = state.cycles_observed + 1
        self._states[wallet] = HysteresisState(
            direction=state.direction,
            target_wallet=state.target_wallet,
            cycles_observed=new_count,
            first_observed_at=state.first_observed_at,
            last_delta=current_delta,
            metadata=dict(metadata) if metadata is not None else dict(state.metadata),
        )
        log.debug(
            "hysteresis_tick",
            wallet=wallet,
            direction=direction,
            cycles=new_count,
            delta=round(current_delta, 4),
        )
        return new_count

    def reset(self, wallet_address: str) -> None:
        """Efface le compteur pour ``wallet``. No-op si absent."""
        wallet = wallet_address.lower()
        if wallet in self._states:
            del self._states[wallet]
            log.debug("hysteresis_reset", wallet=wallet)

    def count(self, wallet_address: str) -> int:
        """Retourne le nb de cycles consécutifs observés pour ``wallet`` (0 si absent)."""
        state = self._states.get(wallet_address.lower())
        return state.cycles_observed if state is not None else 0

    def get(self, wallet_address: str) -> HysteresisState | None:
        """Retourne l'état complet pour ``wallet``, ou ``None`` si absent."""
        return self._states.get(wallet_address.lower())

    def snapshot(self) -> dict[str, HysteresisState]:
        """Copie défensive pour les tests et le debug (jamais utilisée en prod)."""
        return dict(self._states)

    def __len__(self) -> int:
        return len(self._states)
