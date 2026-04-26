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
from typing import Any

import pytest

from polycopy.discovery.dtos import RawPosition
from polycopy.discovery.metrics_collector_v2 import _compute_brier, _compute_zombie_ratio


def _position(
    *,
    initial_value: float = 1000.0,
    current_value: float = 100.0,
    cash_pnl: float = 0.0,
    realized_pnl: float = 0.0,
    redeemable: bool = False,
    opened_at: datetime | None = None,
    asset: str = "0xasset",
    avg_price: float = 0.5,
    outcome_index: int | None = 0,
) -> RawPosition:
    """Helper : construit un `RawPosition` avec defaults raisonnables.

    `redeemable=False` par défaut → `is_resolved=False` (utile aux tests
    zombie qui exigent positions non liquidées). Tests Brier passent
    `redeemable=True` explicitement.
    """
    return RawPosition(
        condition_id="0xcond",
        asset=asset,
        size=10.0,
        avg_price=avg_price,
        initial_value=initial_value,
        current_value=current_value,
        cash_pnl=cash_pnl,
        realized_pnl=realized_pnl,
        total_bought=initial_value,
        redeemable=redeemable,
        opened_at=opened_at,
        outcome_index=outcome_index,
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
    assert (
        _compute_zombie_ratio(
            [recent_zombie, old_zombie, old_healthy],
            now=now,
        )
        == 0.5
    )


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


# --- M14 MA.4 : Brier P(YES) (Gneiting-Raftery 2007) -------------------------


def test_brier_computes_prob_yes_not_prob_side_bought() -> None:
    """MA.4 : Brier sur P(YES) symétrique entre BUY YES et BUY NO.

    Position 1 : BUY YES @ 0.40, YES gagne (cash_pnl > 0).
      yes_at_entry = 0.40, yes_won = 1.0
      sq_error = (1 - 0.40)² = 0.36

    Position 2 : BUY NO @ 0.60, NO gagne (cash_pnl > 0 = side_won).
      yes_at_entry = 1 - 0.60 = 0.40, yes_won = 0.0 (NO won = YES lost)
      sq_error = (0 - 0.40)² = 0.16

    Brier = mean([0.36, 0.16]) = 0.26.
    """
    positions = [
        # Padding pour atteindre _BRIER_MIN_RESOLVED=5.
        _position(asset="p3", avg_price=0.5, outcome_index=0, cash_pnl=1.0, redeemable=True),
        _position(asset="p4", avg_price=0.5, outcome_index=0, cash_pnl=1.0, redeemable=True),
        _position(asset="p5", avg_price=0.5, outcome_index=0, cash_pnl=1.0, redeemable=True),
        # BUY YES @ 0.40, won.
        _position(asset="p1", avg_price=0.40, outcome_index=0, cash_pnl=10.0, redeemable=True),
        # BUY NO @ 0.60, won.
        _position(asset="p2", avg_price=0.60, outcome_index=1, cash_pnl=10.0, redeemable=True),
    ]
    brier = _compute_brier(positions)
    assert brier is not None
    # 3 padding @ 0.5 (yes_won=1, yes_at=0.5 → 0.25 each), + 0.36 + 0.16
    expected = (0.25 * 3 + 0.36 + 0.16) / 5
    assert brier == pytest.approx(expected, abs=1e-6)


def test_brier_symmetric_between_buy_yes_and_buy_no_at_equivalent_prob() -> None:
    """MA.4 : un wallet BUY YES @ 0.30 et un wallet BUY NO @ 0.70 ont le même
    Brier (probabilité YES équivalente côté marché)."""
    # 5 positions BUY YES @ 0.30, YES gagne à chaque fois.
    yes_positions = [
        _position(
            asset=f"yes_{i}",
            avg_price=0.30,
            outcome_index=0,
            cash_pnl=1.0,  # side won
            redeemable=True,
        )
        for i in range(5)
    ]
    # 5 positions BUY NO @ 0.70, NO gagne à chaque fois (= YES perd).
    no_positions = [
        _position(
            asset=f"no_{i}",
            avg_price=0.70,
            outcome_index=1,
            cash_pnl=1.0,  # side won (NO)
            redeemable=True,
        )
        for i in range(5)
    ]
    brier_yes = _compute_brier(yes_positions)
    brier_no = _compute_brier(no_positions)
    # yes_positions : yes_at = 0.30, yes_won = 1.0 → sq_error = (1-0.30)² = 0.49
    # no_positions : yes_at = 1-0.70 = 0.30, yes_won = 0.0 → sq_error = (0-0.30)² = 0.09
    # Ils ne sont PAS égaux en valeur car les outcomes sont différents (YES gagne
    # vs YES perd), mais ils sont sur la même échelle P(YES) — c'est ça l'invariant.
    # Test plus pertinent : les 2 valeurs sont bien dérivées de la même formule
    # P(YES_at_entry).
    assert brier_yes == pytest.approx(0.49)
    assert brier_no == pytest.approx(0.09)
    # Invariant clé : pour 2 positions à P(YES_at_entry) = 0.30 ET résolution YES,
    # les deux côtés (BUY YES + BUY NO) doivent donner exactement le même Brier.
    sym_yes = [
        _position(
            asset="sym_y",
            avg_price=0.30,
            outcome_index=0,
            cash_pnl=1.0,
            redeemable=True,
        ),
    ] * 5
    sym_no_yes_won = [
        # BUY NO @ 0.70 → P(YES) = 0.30 ; YES gagne → cash_pnl < 0 (NO perd).
        _position(
            asset="sym_n",
            avg_price=0.70,
            outcome_index=1,
            cash_pnl=-1.0,
            redeemable=True,
        ),
    ] * 5
    sym_brier_yes = _compute_brier(sym_yes)
    sym_brier_no_yes_won = _compute_brier(sym_no_yes_won)
    assert sym_brier_yes == pytest.approx(sym_brier_no_yes_won)


def test_brier_skips_positions_with_missing_outcome_index() -> None:
    """MA.4 : positions sans `outcome_index` (Data API legacy) → skip.

    Si trop de positions sans outcome_index, le Brier devient None
    (insuffisant).
    """
    positions = [
        _position(asset=f"p{i}", outcome_index=None, cash_pnl=1.0, redeemable=True)
        for i in range(10)
    ]
    assert _compute_brier(positions) is None


def test_brier_returns_none_on_insufficient_resolved() -> None:
    """MA.4 : moins de _BRIER_MIN_RESOLVED=5 positions résolues → None."""
    positions = [
        _position(asset=f"p{i}", avg_price=0.5, outcome_index=0, cash_pnl=1.0, redeemable=True)
        for i in range(3)
    ]
    assert _compute_brier(positions) is None


# --- M15 MB.1 : _compute_internal_pnl_score sigmoid (4 tests §9.1) ----------


def _build_collector_with_repo(
    my_position_repo: Any,  # MyPositionRepository
    *,
    execution_mode: str = "dry_run",
    min_positions: int = 10,
    scale_usd: str = "10.0",
):
    """Helper : construit un MetricsCollectorV2 minimaliste pour tester
    `_compute_internal_pnl_score` isolément. Tous les autres deps sont des
    mocks AsyncMock car la méthode ne les touche pas."""
    from decimal import Decimal
    from unittest.mock import AsyncMock, MagicMock

    from polycopy.config import Settings
    from polycopy.discovery.metrics_collector_v2 import MetricsCollectorV2

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        execution_mode=execution_mode,
        scoring_internal_min_positions=min_positions,
        scoring_internal_pnl_scale_usd=Decimal(scale_usd),
    )
    return MetricsCollectorV2(
        base_collector=MagicMock(),
        daily_pnl_repo=AsyncMock(),
        data_api=AsyncMock(),
        category_resolver=AsyncMock(),
        settings=settings,
        my_positions_repo=my_position_repo,
    )


async def _seed_my_position_closed(
    my_position_repo,
    *,
    source_wallet: str,
    realized_pnl: float,
    closed_at: datetime,
    simulated: bool = True,
    asset_id: str | None = None,
    condition_id: str | None = None,
) -> None:
    """Insère une position closed avec ``realized_pnl`` + ``source_wallet_address``.

    On passe par le repo public ``upsert_virtual`` pour BUY puis SELL afin de
    cristalliser ``realized_pnl`` ; pour la précision des tests on insère une
    position pré-fermée directement via session pour contrôler le timestamp.
    """
    from polycopy.storage.models import MyPosition

    # Bypass repository : on contrôle exactement closed_at et realized_pnl.
    async with my_position_repo._session_factory() as session:  # noqa: SLF001
        position = MyPosition(
            condition_id=condition_id or f"0xcond_{closed_at.timestamp():.0f}",
            asset_id=asset_id or f"0xasset_{closed_at.timestamp():.0f}",
            size=0.0,
            avg_price=0.5,
            simulated=simulated,
            closed_at=closed_at,
            realized_pnl=realized_pnl,
            source_wallet_address=source_wallet.lower(),
        )
        session.add(position)
        await session.commit()


async def test_internal_pnl_score_sigmoid_bounds(
    my_position_repo,
) -> None:
    """MB.1 §9.1 #1 — sigmoid borné, +$50 sur 12 positions → ~0.9933."""
    from math import exp

    wallet = "0xWALLET_PNL_HIGH"
    now = datetime.now(tz=UTC)
    # 12 positions closed avec réalisé total = +$50 sur 12 closed récentes.
    per_position = 50.0 / 12.0
    for i in range(12):
        await _seed_my_position_closed(
            my_position_repo,
            source_wallet=wallet,
            realized_pnl=per_position,
            closed_at=now - timedelta(days=i),
            simulated=True,
            asset_id=f"0xa{i}",
            condition_id=f"0xc{i}",
        )

    collector = _build_collector_with_repo(my_position_repo, execution_mode="dry_run")
    score = await collector._compute_internal_pnl_score(wallet)

    assert score is not None
    expected = 1.0 / (1.0 + exp(-5.0))  # 50/10 = 5 → sigmoid(5) ≈ 0.9933
    assert score == pytest.approx(expected, abs=1e-6)


async def test_internal_pnl_score_returns_none_under_min_positions(
    my_position_repo,
) -> None:
    """MB.1 §9.1 #2 — count<10 → None (cold-start)."""
    wallet = "0xWALLET_TOO_FEW"
    now = datetime.now(tz=UTC)
    for i in range(9):  # 1 sous le seuil 10
        await _seed_my_position_closed(
            my_position_repo,
            source_wallet=wallet,
            realized_pnl=1.0,
            closed_at=now - timedelta(days=i),
            simulated=True,
            asset_id=f"0xa{i}",
            condition_id=f"0xc{i}",
        )

    collector = _build_collector_with_repo(my_position_repo, execution_mode="dry_run")
    score = await collector._compute_internal_pnl_score(wallet)
    assert score is None


async def test_internal_pnl_score_dry_run_vs_live_mode_isolation(
    my_position_repo,
) -> None:
    """MB.1 §9.1 #3 — ségrégation simulated selon execution_mode."""
    wallet = "0xWALLET_MIXED_MODES"
    now = datetime.now(tz=UTC)
    # 12 positions simulated=True (dry-run) avec PnL +$24.
    for i in range(12):
        await _seed_my_position_closed(
            my_position_repo,
            source_wallet=wallet,
            realized_pnl=2.0,
            closed_at=now - timedelta(days=i),
            simulated=True,
            asset_id=f"0xdry_a{i}",
            condition_id=f"0xdry_c{i}",
        )
    # 12 positions simulated=False (live) avec PnL -$12.
    for i in range(12):
        await _seed_my_position_closed(
            my_position_repo,
            source_wallet=wallet,
            realized_pnl=-1.0,
            closed_at=now - timedelta(days=i),
            simulated=False,
            asset_id=f"0xlive_a{i}",
            condition_id=f"0xlive_c{i}",
        )

    score_dry = await _build_collector_with_repo(
        my_position_repo, execution_mode="dry_run"
    )._compute_internal_pnl_score(wallet)
    score_live = await _build_collector_with_repo(
        my_position_repo, execution_mode="live"
    )._compute_internal_pnl_score(wallet)

    assert score_dry is not None and score_live is not None
    # dry_run lit simulated=True → +$24/10 = 2.4 → sigmoid(2.4) ≈ 0.917
    # live    lit simulated=False → -$12/10 = -1.2 → sigmoid(-1.2) ≈ 0.231
    assert score_dry > 0.5 > score_live  # filtre simulated correct


async def test_internal_pnl_score_30d_window_correct(
    my_position_repo,
) -> None:
    """MB.1 §9.1 #4 — fenêtre 30j filtre les anciennes positions.

    13 closed total, mais 5 récentes (<30j) + 8 anciennes (>30j) → après
    filtre 30j il reste 5 < 10 = seuil min → cold-start None.
    """
    wallet = "0xWALLET_WINDOW"
    now = datetime.now(tz=UTC)
    # 5 récentes (15j → in window)
    for i in range(5):
        await _seed_my_position_closed(
            my_position_repo,
            source_wallet=wallet,
            realized_pnl=2.0,
            closed_at=now - timedelta(days=15),
            simulated=True,
            asset_id=f"0xrecent_a{i}",
            condition_id=f"0xrecent_c{i}",
        )
    # 8 anciennes (45j → out of 30d window)
    for i in range(8):
        await _seed_my_position_closed(
            my_position_repo,
            source_wallet=wallet,
            realized_pnl=2.0,
            closed_at=now - timedelta(days=45),
            simulated=True,
            asset_id=f"0xold_a{i}",
            condition_id=f"0xold_c{i}",
        )

    collector = _build_collector_with_repo(my_position_repo, execution_mode="dry_run")
    score = await collector._compute_internal_pnl_score(wallet)
    assert score is None  # 5 in window < 10 min
