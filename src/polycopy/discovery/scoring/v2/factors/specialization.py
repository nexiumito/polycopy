"""Facteur ``specialization`` (M14 MA.5 — flip HHI signal).

``HHI(volume par catégorie Gamma)`` — **signal direct**, pas inversé.

Un wallet concentré ≥ 70 % sur 1-2 catégories (``HHI ≥ 0.49``) est un
corrélat empirique de l'avantage informationnel (Mitts & Ofir 2026 sur
Polymarket : insider wallets = 69.9 % WR > 60σ above chance, HHI → 1.0
sur les marchés de leur expertise — Iran strike, Taylor Swift, Magamyman).

**Changement M14 vs M12** : la formule M12 ``1 - HHI`` pénalisait la
concentration, ce qui était un anti-pattern (Claude C9, deep-search F07).
On *récompense* maintenant directement le HHI. Les wallets diversifiés
ne sont plus pénalisés négativement, ils sont juste moins récompensés.

**Différence vs v1 (M5)** : v1 calcule HHI sur `condition_id` (marché
individuel). v2.1 calcule HHI sur **catégorie** Gamma top-level
(Politics / Sports / Crypto / Geopolitics / Economy / Tech / ...).
Mapping ``condition_id → category`` construit amont par
:class:`MetricsCollectorV2` via Gamma ``?include_tag=true``.

Pondération inchangée vs M12 (0.15 → 0.1875 post-renormalisation MA.1).
On inverse le sens, pas l'amplitude — pour une amplification "insider-first"
voir MF (Mitts-Ofir composite complet).

Pure function — consomme uniquement :attr:`TraderMetricsV2.hhi_categories`.

Cf. spec M14 §5.5 (MA.5) + Claude C9 + Mitts & Ofir 2026
https://corpgov.law.harvard.edu/2026/03/25/from-iran-to-taylor-swift-informed-trading-in-prediction-markets/
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2


def compute_specialization(metrics: TraderMetricsV2) -> float:
    """Retourne ``HHI_categories`` direct, clippé ``[0, 1]``.

    HHI ∈ ``[0, 1]`` par construction (somme de carrés de parts). Le clip
    est défense en profondeur contre des valeurs hors-bornes (théoriquement
    impossibles si le calcul amont est correct).

    M14 MA.5 : signe inversé vs M12. High HHI = insider concentration =
    récompense.
    """
    return max(0.0, min(1.0, metrics.hhi_categories))
