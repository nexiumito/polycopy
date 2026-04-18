"""Winsorisation p5-p95 + normalisation pool-wide (M12 §3.8).

Pure functions — aucun I/O, aucun state. Testables isolément. Contrats :

- **Déterminisme** : même entrée → même sortie (garanti par la pure function).
- **Bornes** : la sortie de :func:`apply_pool_normalization` ∈ ``[0.0, 1.0]``.
- **Monotonicité** : si ``a ≤ b`` et pool fixé, alors ``apply(a) ≤ apply(b)``.

Rationale winsorisation p5-p95 : absorbe les outliers (ex: Fredi9999 avec
Sortino ≈ 50 sur l'élection 2024) sans supprimer les queues entièrement.
"""

from __future__ import annotations


def winsorize_p5_p95(values: list[float]) -> tuple[float, float]:
    """Retourne ``(p5, p95)`` calculés sur ``values``.

    Pool vide → retourne ``(0.0, 1.0)`` (sentinel qui produit une normalisation
    "identité" dans :func:`apply_pool_normalization`). Déterministe : même
    entrée → même sortie.

    Méthode percentile : ``p5 = sorted[int(0.05*n)]``, ``p95 =
    sorted[int(0.95*n)]``. Simple, reproductible, cohérent avec numpy
    "lower" method.
    """
    if not values:
        return (0.0, 1.0)
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    idx_p5 = min(max(0, int(0.05 * n)), n - 1)
    idx_p95 = min(max(0, int(0.95 * n)), n - 1)
    return (sorted_vals[idx_p5], sorted_vals[idx_p95])


def apply_pool_normalization(
    wallet_value: float,
    pool_values: list[float],
) -> float:
    """Clippe ``wallet_value`` à ``(p5, p95)`` puis rescale dans ``[0, 1]``.

    - Pool vide → clip direct ``wallet_value`` à ``[0, 1]``.
    - Pool dégénéré (``p95 == p5``) → retourne 0.5 (sentinel : pool plat =
      pas de discrimination possible).
    - Sinon → ``(clip(value, p5, p95) - p5) / (p95 - p5)``.

    Idempotent : deux appels successifs sur la même valeur donnent le même
    résultat (après le 1er call, value ∈ [0, 1] donc clip ne la touche plus).
    """
    if not pool_values:
        return max(0.0, min(1.0, wallet_value))
    p5, p95 = winsorize_p5_p95(pool_values)
    if p95 <= p5:
        return 0.5
    clipped = max(p5, min(p95, wallet_value))
    return (clipped - p5) / (p95 - p5)
