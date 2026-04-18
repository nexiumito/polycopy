"""Tests de la formule de scoring M5 v1."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from polycopy.config import Settings
from polycopy.discovery.dtos import TraderMetrics
from polycopy.discovery.scoring import compute_score


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "target_wallets": "0xdummy",
        "scoring_version": "v1",
        "scoring_min_closed_markets": 10,
        "scoring_lookback_days": 90,
        "scoring_promotion_threshold": 0.65,
        "scoring_demotion_threshold": 0.40,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _metrics(
    *,
    resolved: int = 20,
    win_rate: float = 0.6,
    realized_roi: float = 0.2,
    herfindahl: float = 0.3,
    volume: float = 50_000,
) -> TraderMetrics:
    return TraderMetrics(
        wallet_address="0xtest",
        resolved_positions_count=resolved,
        open_positions_count=2,
        win_rate=win_rate,
        realized_roi=realized_roi,
        total_volume_usd=volume,
        herfindahl_index=herfindahl,
        nb_distinct_markets=5,
        largest_position_value_usd=1000.0,
        measurement_window_days=90,
        fetched_at=datetime.now(tz=UTC),
    )


def test_cold_start_returns_zero_and_low_confidence() -> None:
    score, low_conf = compute_score(
        _metrics(resolved=5),
        settings=_settings(),
    )
    assert score == 0.0
    assert low_conf is True


def test_near_perfect_profile_scores_high() -> None:
    score, low_conf = compute_score(
        _metrics(
            resolved=40,
            win_rate=1.0,
            realized_roi=2.0,
            herfindahl=0.0,
            volume=10_000_000,
        ),
        settings=_settings(),
    )
    assert low_conf is False
    # 0.3*1 + 0.3*1 + 0.2*1 + 0.2*1 = 1.0 (capé par le clip final).
    assert score == pytest.approx(1.0, abs=0.01)


def test_terrible_profile_scores_low() -> None:
    score, low_conf = compute_score(
        _metrics(
            resolved=20,
            win_rate=0.0,
            realized_roi=-2.0,
            herfindahl=1.0,
            volume=0.0,
        ),
        settings=_settings(),
    )
    assert low_conf is False
    assert score == pytest.approx(0.0, abs=0.01)


def test_wash_trading_profile_below_promotion() -> None:
    """Scénario wash : win_rate=0.5, roi~=0, hhi=0.5, volume moyen.

    Score attendu ≈ 0.3*0.5 + 0.3*0.5 + 0.2*0.5 + 0.2*log10(50) / 3 ≈ 0.55 < 0.65.
    """
    score, _ = compute_score(
        _metrics(
            resolved=20,
            win_rate=0.5,
            realized_roi=0.0,
            herfindahl=0.5,
            volume=50_000,
        ),
        settings=_settings(),
    )
    assert 0.4 <= score < 0.65


def test_whale_one_market_blocked_by_diversity() -> None:
    """Trader 1-marché : diversity=0 limite fortement le score."""
    score, _ = compute_score(
        _metrics(
            resolved=20,
            win_rate=1.0,
            realized_roi=0.5,
            herfindahl=1.0,  # tout sur 1 marché
            volume=1_000_000,
        ),
        settings=_settings(),
    )
    # 0.3*1 + 0.3*0.625 + 0.2*0 + 0.2*1 = 0.6875 — haut mais borderline.
    # L'important : diversity=0 empêche d'atteindre 1.0.
    assert score < 0.80


def test_unknown_scoring_version_raises() -> None:
    s = _settings(scoring_version="vZ")
    with pytest.raises(ValueError, match="Unknown SCORING_VERSION"):
        compute_score(_metrics(resolved=20), settings=s)


@pytest.mark.parametrize("roi", [-5.0, -2.0, -1.0, 0.0, 1.0, 2.0, 5.0])
@pytest.mark.parametrize("hhi", [0.0, 0.3, 0.7, 1.0])
@pytest.mark.parametrize("vol", [0.0, 1.0, 1_000, 10_000_000, 1e12])
def test_score_always_in_unit_interval(roi: float, hhi: float, vol: float) -> None:
    """Property-style : quelle que soit la combinaison, score ∈ [0, 1]."""
    score, _ = compute_score(
        _metrics(
            resolved=20,
            win_rate=0.5,
            realized_roi=roi,
            herfindahl=hhi,
            volume=vol,
        ),
        settings=_settings(),
    )
    assert 0.0 <= score <= 1.0
