"""Facteur ``specialization`` (M12 §3.5).

``1 - HHI(volume par catégorie Gamma)``.

Un wallet concentré ≥ 70 % sur 1-2 catégories (``HHI ≥ 0.49``) est un
corrélat empirique de l'avantage informationnel (arxiv 2603.03136).

**Différence vs v1** : v1 calcule HHI sur `condition_id` (marché individuel)
[metrics_collector.py:87](metrics_collector.py). v2 calcule HHI sur **catégorie**
(Politics / Sports / Crypto / Geopolitics / Economy / Tech / ...). Mapping
``condition_id → category`` construit amont par :class:`MetricsCollectorV2`
via Gamma ``?include_tag=true`` + matching sur
``_TOP_LEVEL_POLYMARKET_CATEGORIES``.

Pure function — consomme uniquement :attr:`TraderMetricsV2.hhi_categories`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2


def compute_specialization(metrics: TraderMetricsV2) -> float:
    """Retourne ``1 - HHI_categories``, clippé ``[0, 1]``.

    HHI ∈ ``[0, 1]`` par construction (somme de carrés de parts), donc
    ``1 - HHI`` ∈ ``[0, 1]``. Le clip est défense en profondeur.
    """
    return max(0.0, min(1.0, 1.0 - metrics.hhi_categories))
