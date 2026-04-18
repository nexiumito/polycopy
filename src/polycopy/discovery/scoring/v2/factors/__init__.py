"""Sous-package factors : les 6 facteurs purs du scoring v2 (M12 §3.2-3.7).

Chaque facteur est une pure function ``compute_<factor>(metrics_v2) -> float``
qui retourne la valeur brute du sous-score (pré-normalisation pool-wide). La
normalisation p5-p95 + rescale ``[0, 1]`` est déléguée à
:mod:`polycopy.discovery.scoring.v2.normalization`.

Design (spec M12 §3) :

- :func:`compute_risk_adjusted` — Sortino (0.6x) + Calmar (0.4x) sur l'equity
  curve 90j.
- :func:`compute_calibration` — Brier-skill score sur les positions résolues.
- :func:`compute_timing_alpha` — wrapper sur la valeur pré-calculée par
  :class:`MetricsCollectorV2` (pair-level aggregation par sqrt(n_trades)).
- :func:`compute_specialization` — ``1 - HHI(volume par catégorie Gamma)``.
- :func:`compute_consistency` — fraction de mois PnL>0 sur 3 mois.
- :func:`compute_discipline` — ``(1 - zombie_ratio) × sizing_stability``.
"""

from __future__ import annotations

from polycopy.discovery.scoring.v2.factors.calibration import compute_calibration
from polycopy.discovery.scoring.v2.factors.consistency import compute_consistency
from polycopy.discovery.scoring.v2.factors.discipline import compute_discipline
from polycopy.discovery.scoring.v2.factors.risk_adjusted import compute_risk_adjusted
from polycopy.discovery.scoring.v2.factors.specialization import compute_specialization
from polycopy.discovery.scoring.v2.factors.timing_alpha import compute_timing_alpha

__all__ = [
    "compute_calibration",
    "compute_consistency",
    "compute_discipline",
    "compute_risk_adjusted",
    "compute_specialization",
    "compute_timing_alpha",
]
