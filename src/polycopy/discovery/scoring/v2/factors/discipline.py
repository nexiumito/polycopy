"""Facteur ``discipline`` (M12 §3.7).

``(1 - zombie_ratio) × sizing_stability``.

- ``zombie_ratio`` (Gemini §1.1) : proportion du capital dans positions <
  2 % × initial_value **non liquidées**, sur fenêtre 90j, excluant les
  positions ouvertes depuis < 30j (pour ne pas pénaliser les wallets récents).
- ``sizing_stability = 1 - min(1.0, sizing_cv)`` : inverse du coefficient of
  variation des tailles de trade. Un wallet discipliné fait des bets cohérents,
  pas des sizing pseudo-aléatoires.

Zombie ratio ≥ 0.40 est **déjà filtré** par le gate correspondant (§4.1).
Les wallets qui passent les gates ont zombie_ratio < 0.40 → anti_zombie ∈
``(0.6, 1.0]``. La pool normalization discriminera à l'intérieur de cette
fenêtre.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2


def compute_discipline(metrics: TraderMetricsV2) -> float:
    """Retourne ``(1 - zombie) × sizing_stability`` clippé ``[0, 1]``."""
    anti_zombie = max(0.0, min(1.0, 1.0 - metrics.zombie_ratio))
    sizing_stability = max(0.0, min(1.0, 1.0 - metrics.sizing_cv))
    return max(0.0, min(1.0, anti_zombie * sizing_stability))
