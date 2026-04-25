"""Tests des helpers purs du :mod:`polycopy.discovery.metrics_collector_v2`.

Ces tests couvrent uniquement les pure functions module-level
(``_compute_zombie_ratio``, ``_compute_brier``, etc.) — pas la classe
:class:`MetricsCollectorV2` qui nécessiterait des mocks I/O.

Couverture M14 :

- MA.4 : ``_compute_brier`` — P(YES) au lieu de P(side_bought).
- MA.6 : ``_compute_zombie_ratio`` — filtre temporel <30j (audit H-014).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from polycopy.discovery.dtos import RawPosition
from polycopy.discovery.metrics_collector_v2 import _compute_zombie_ratio


def _position(
    *,
    initial_value: float = 1000.0,
    current_value: float = 100.0,
    cash_pnl: float = 0.0,
    realized_pnl: float = 0.0,
    redeemable: bool = False,
    opened_at: datetime | None = None,
    asset: str = "0xasset",
) -> RawPosition:
    """Helper : construit un `RawPosition` avec defaults raisonnables."""
    return RawPosition(
        condition_id="0xcond",
        asset=asset,
        size=10.0,
        avg_price=0.5,
        initial_value=initial_value,
        current_value=current_value,
        cash_pnl=cash_pnl,
        realized_pnl=realized_pnl,
        total_bought=initial_value,
        redeemable=redeemable,
        opened_at=opened_at,
    )


# --- M14 MA.6 : zombie_ratio temporal <30d filter ----------------------------


def test_zombie_ratio_excludes_positions_opened_within_30d() -> None:
    """MA.6 : positions ouvertes < 30j EXCLUES du dénominateur (fix H-014)."""
    now = datetime(2026, 4, 25, tzinfo=UTC)
    recent_zombie = _position(
        asset="recent",
        opened_at=now - timedelta(days=5),
        initial_value=1000.0,
        current_value=5.0,  # zombie : 0.5% < 2%
    )
    old_zombie = _position(
        asset="old",
        opened_at=now - timedelta(days=90),
        initial_value=1000.0,
        current_value=5.0,  # zombie : 0.5% < 2%
    )

    # Cas 1 : seule old_zombie est éligible → ratio = 1.0 (100% du capital
    # éligible est zombie).
    assert _compute_zombie_ratio([recent_zombie, old_zombie], now=now) == 1.0

    # Cas 2 : si on filtre la recent_zombie ET on ajoute une old saine, le
    # ratio devient 50%.
    old_healthy = _position(
        asset="healthy",
        opened_at=now - timedelta(days=90),
        initial_value=1000.0,
        current_value=600.0,
    )
    assert _compute_zombie_ratio(
        [recent_zombie, old_zombie, old_healthy],
        now=now,
    ) == 0.5


def test_zombie_ratio_fallback_inclusion_on_missing_opened_at() -> None:
    """MA.6 : Data API ne fournit pas `opened_at` → fallback inclusion (M12 compat).

    Si on excluait conservativement (D5 stricte), tous les wallets actuels
    auraient `zombie_ratio=0.0` (Data API actuelle ne fournit jamais
    `opened_at`) → facteur inopérant. Décision pragmatique D5-bis :
    `opened_at=None` → position incluse (comportement M12 préservé).
    """
    zombie = _position(
        opened_at=None,
        initial_value=1000.0,
        current_value=10.0,  # 1% < 2%
    )
    healthy = _position(
        opened_at=None,
        initial_value=1000.0,
        current_value=500.0,
        asset="healthy",
    )
    # 50 % du capital (l'un des 2) est zombie.
    assert _compute_zombie_ratio([zombie, healthy]) == 0.5


def test_zombie_ratio_no_eligible_returns_zero() -> None:
    """MA.6 : si aucune position éligible (tout < 30j), retourne 0.0 (pas div0)."""
    now = datetime(2026, 4, 25, tzinfo=UTC)
    recents = [
        _position(
            asset=f"recent_{i}",
            opened_at=now - timedelta(days=i + 1),
            initial_value=1000.0,
            current_value=5.0,
        )
        for i in range(5)
    ]
    assert _compute_zombie_ratio(recents, now=now) == 0.0
