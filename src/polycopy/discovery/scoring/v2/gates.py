"""Gates durs pré-scoring v2 (M12 §4).

6 gates purs qui valident un wallet **avant** tout calcul de score v2. Un
wallet qui échoue un gate est **jamais scoré** (économie compute) + une row
``trader_events.event_type="gate_rejected"`` est écrite pour l'audit trail
(cf. :class:`DiscoveryOrchestrator` M12 integration).

Ordre des gates (dans :func:`check_all_gates`) optimisé pour le coût moyen
d'un fail :

1. ``not_blacklisted`` — env lookup, O(1).
2. ``not_wash_cluster`` — env lookup, O(1).
3. ``days_active_min`` — DTO lookup, O(1). Cold start mode relâche à 7.
4. ``trade_count_min`` — DTO lookup, O(1). Cold start mode relâche à 20.
5. ``cash_pnl_positive`` — DTO lookup, O(1).
6. ``zombie_ratio_max`` — DTO lookup, O(1).

Chaque gate retourne un :class:`GateResult` avec ``observed_value``,
``threshold`` et ``reason`` — exploitable tel quel par l'audit trail et le
message de rejet humain (dashboard, logs).

**Invariant** : ``check_all_gates`` est **fail-fast** — court-circuite au
premier gate échoué et ne calcule pas les suivants.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from polycopy.discovery.scoring.v2.dtos import (
    AggregateGateResult,
    GateResult,
)

if TYPE_CHECKING:
    from polycopy.config import Settings
    from polycopy.discovery.scoring.v2.dtos import TraderMetricsV2


# Seuils fixes — figés par la spec M12 §4.1. Changer un seuil = bumper
# SCORING_VERSION (append-only, versioning sacré).
_CASH_PNL_MIN: float = 0.0
_TRADE_COUNT_MIN: int = 50
_TRADE_COUNT_MIN_COLD_START: int = 20
_DAYS_ACTIVE_MIN: int = 30
_DAYS_ACTIVE_MIN_COLD_START: int = 7
_ZOMBIE_RATIO_MAX: float = 0.40


def check_cash_pnl(metrics: TraderMetricsV2) -> GateResult:
    """Gate 1 : ``cash_pnl_90d > 0``.

    Reichenbach-Walther : 70 % des traders perdent → filtre trivial très
    efficace.
    """
    observed = float(metrics.cash_pnl_90d)
    passed = observed > _CASH_PNL_MIN
    return GateResult(
        gate_name="cash_pnl_positive",
        passed=passed,
        observed_value=observed,
        threshold=_CASH_PNL_MIN,
        reason=(
            f"cash_pnl_90d:{observed:.2f} > {_CASH_PNL_MIN}"
            if passed
            else f"cash_pnl_90d:{observed:.2f} <= {_CASH_PNL_MIN}"
        ),
    )


def check_trade_count(metrics: TraderMetricsV2, *, cold_start_mode: bool) -> GateResult:
    """Gate 2 : ``trade_count_90d ≥ 50`` (ou 20 si cold_start_mode).

    ``cold_start_mode=True`` relâche le gate pour ne pas exclure les wallets
    jeunes mais intéressants. Documenté warning au boot si activé.
    """
    threshold = _TRADE_COUNT_MIN_COLD_START if cold_start_mode else _TRADE_COUNT_MIN
    observed = int(metrics.trade_count_90d)
    passed = observed >= threshold
    suffix = " (cold_start_mode)" if cold_start_mode else ""
    return GateResult(
        gate_name="trade_count_min",
        passed=passed,
        observed_value=observed,
        threshold=threshold,
        reason=(
            f"trade_count_90d:{observed} >= {threshold}{suffix}"
            if passed
            else f"trade_count_90d:{observed} < {threshold}{suffix}"
        ),
    )


def check_days_active(metrics: TraderMetricsV2, *, cold_start_mode: bool = False) -> GateResult:
    """Gate 3 : ``days_active ≥ 30`` (ou 7 si cold_start_mode). Anti-Sybil basique.

    ``cold_start_mode=True`` relâche le gate pour ne pas exclure les wallets
    récemment actifs dans un pool jeune (dev/test). Documenté warning au boot
    si activé. Cohérent avec le relâchement de ``trade_count_min``.
    """
    threshold = _DAYS_ACTIVE_MIN_COLD_START if cold_start_mode else _DAYS_ACTIVE_MIN
    observed = int(metrics.days_active)
    passed = observed >= threshold
    suffix = " (cold_start_mode)" if cold_start_mode else ""
    return GateResult(
        gate_name="days_active_min",
        passed=passed,
        observed_value=observed,
        threshold=threshold,
        reason=(
            f"days_active:{observed} >= {threshold}{suffix}"
            if passed
            else f"days_active:{observed} < {threshold}{suffix}"
        ),
    )


def check_zombie_ratio(metrics: TraderMetricsV2) -> GateResult:
    """Gate 4 : ``zombie_ratio < 0.40``. Anti-manipulation win rate."""
    observed = float(metrics.zombie_ratio)
    passed = observed < _ZOMBIE_RATIO_MAX
    return GateResult(
        gate_name="zombie_ratio_max",
        passed=passed,
        observed_value=observed,
        threshold=_ZOMBIE_RATIO_MAX,
        reason=(
            f"zombie_ratio:{observed:.3f} < {_ZOMBIE_RATIO_MAX}"
            if passed
            else f"zombie_ratio:{observed:.3f} >= {_ZOMBIE_RATIO_MAX}"
        ),
    )


def check_not_blacklisted(wallet: str, settings: Settings) -> GateResult:
    """Gate 5 : wallet hors ``BLACKLISTED_WALLETS``. Défense en profondeur
    (M5 filtre déjà au candidate_pool, mais re-check au moment du scoring
    évite les fuites si BLACKLISTED_WALLETS change en cours de cycle).
    """
    blacklist = {w.lower() for w in settings.blacklisted_wallets}
    observed = wallet.lower()
    passed = observed not in blacklist
    return GateResult(
        gate_name="not_blacklisted",
        passed=passed,
        observed_value=observed,
        threshold="not_in_blacklist",
        reason=(
            "wallet not in BLACKLISTED_WALLETS" if passed else f"wallet:{observed} is blacklisted"
        ),
    )


def check_not_wash_cluster(wallet: str, settings: Settings) -> GateResult:
    """Gate 6 : wallet hors ``WASH_CLUSTER_WALLETS`` (v1 manuel).

    Auto-detection wash cluster reportée M17+ (§14.6 spec). v1 M12 =
    liste ENV maintenue manuellement par l'utilisateur depuis observations
    on-chain.
    """
    wash_cluster: list[str] = list(getattr(settings, "wash_cluster_wallets", []))
    wash_set = {w.lower() for w in wash_cluster}
    observed = wallet.lower()
    passed = observed not in wash_set
    return GateResult(
        gate_name="not_wash_cluster",
        passed=passed,
        observed_value=observed,
        threshold="not_in_wash_cluster",
        reason=(
            "wallet not in WASH_CLUSTER_WALLETS"
            if passed
            else f"wallet:{observed} in WASH_CLUSTER_WALLETS"
        ),
    )


def check_all_gates(
    metrics: TraderMetricsV2,
    wallet: str,
    settings: Settings,
) -> AggregateGateResult:
    """Vérifie les 6 gates en séquence fail-fast.

    Ordre optimisé pour coût moyen d'un fail : env lookups d'abord (O(1)),
    puis DTO lookups. Retourne ``AggregateGateResult(passed=True)`` si tous
    passent ; sinon ``passed=False`` + le premier :class:`GateResult` échoué.
    """
    cold_start_mode: bool = getattr(settings, "scoring_v2_cold_start_mode", False)
    checks: list[Callable[[], GateResult]] = [
        lambda: check_not_blacklisted(wallet, settings),
        lambda: check_not_wash_cluster(wallet, settings),
        lambda: check_days_active(metrics, cold_start_mode=cold_start_mode),
        lambda: check_trade_count(metrics, cold_start_mode=cold_start_mode),
        lambda: check_cash_pnl(metrics),
        lambda: check_zombie_ratio(metrics),
    ]
    for check in checks:
        result = check()
        if not result.passed:
            return AggregateGateResult(passed=False, failed_gate=result)
    return AggregateGateResult(passed=True, failed_gate=None)
