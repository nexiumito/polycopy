"""Tests de la politique de TTL adaptatif ``_cache_policy.compute_ttl`` (M11 §4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from polycopy.strategy._cache_policy import (
    _TTL_ACTIVE_SECONDS,
    _TTL_INACTIVE_SECONDS,
    _TTL_NEAR_RESOLUTION_SECONDS,
    _TTL_RESOLVED_SENTINEL,
    compute_ttl,
)
from polycopy.strategy.dtos import MarketMetadata


def _make_market(**overrides: Any) -> MarketMetadata:
    base: dict[str, Any] = {
        "id": "m1",
        "conditionId": "0xcond",
        "active": True,
        "closed": False,
        "archived": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "liquidityClob": 0.0,
        "clobTokenIds": "[]",
        "outcomes": "[]",
        "outcomePrices": "[]",
    }
    base.update(overrides)
    return MarketMetadata.model_validate(base)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 4, 18, 12, 0, 0, tzinfo=UTC)


def test_cache_policy_resolved_market_infinite_ttl(now: datetime) -> None:
    market = _make_market(closed=True)
    assert compute_ttl(market, now) == _TTL_RESOLVED_SENTINEL


def test_cache_policy_archived_market_infinite_ttl(now: datetime) -> None:
    market = _make_market(archived=True)
    assert compute_ttl(market, now) == _TTL_RESOLVED_SENTINEL


def test_cache_policy_near_resolution_short_ttl(now: datetime) -> None:
    end = now + timedelta(minutes=30)
    market = _make_market(endDate=end.isoformat())
    assert compute_ttl(market, now) == _TTL_NEAR_RESOLUTION_SECONDS


def test_cache_policy_active_market_liquidity(now: datetime) -> None:
    market = _make_market(liquidityClob=5000.0)
    assert compute_ttl(market, now) == _TTL_ACTIVE_SECONDS


def test_cache_policy_active_market_volume(now: datetime) -> None:
    # Volume24hr passe via ``extra="allow"`` sur ``MarketMetadata``.
    market = _make_market(volume24hr=500.0)
    assert compute_ttl(market, now) == _TTL_ACTIVE_SECONDS


def test_cache_policy_inactive_market_long_ttl(now: datetime) -> None:
    market = _make_market()
    assert compute_ttl(market, now) == _TTL_INACTIVE_SECONDS
