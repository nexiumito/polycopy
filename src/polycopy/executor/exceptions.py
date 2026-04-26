"""Exceptions de la couche executor (M17 MD.4 + future).

Centralise les erreurs sentinelles que les writers de monitoring catch
pour décider d'un skip vs un crash. Aucun secret jamais exposé dans les
messages — seulement des `asset_id` publics et des durées numériques.
"""

from __future__ import annotations


class MidpointUnavailableError(RuntimeError):
    """M17 MD.4 — `VirtualWalletStateReader` ne peut pas valoriser une position.

    Levée quand le mid CLOB d'une position virtuelle est manquant ET
    aucun ``last_known`` frais (≤ TTL 10 min) n'est disponible. Catchée
    par ``PnlSnapshotWriter._tick`` qui skip le snapshot — évite d'écrire
    un ``total_usdc`` creux contre lequel un drawdown factice pourrait se
    calculer (audit C-004).
    """

    def __init__(
        self,
        *,
        asset_id: str,
        last_known_age_seconds: float | None,
    ) -> None:
        super().__init__(
            f"Midpoint unavailable for asset_id={asset_id}, "
            f"last_known_age={last_known_age_seconds}s",
        )
        self.asset_id = asset_id
        self.last_known_age_seconds = last_known_age_seconds
