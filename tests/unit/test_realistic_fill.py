"""Tests M8 §9.2 — algorithme pur ``simulate_fill``.

Couvre :
- BUY happy path (avg pondéré).
- BUY size > book depth, allow_partial=False → REJECTED.
- BUY size > book depth, allow_partial=True → fill partiel.
- SELL happy path.
- Book vide → REJECTED ``empty_book``.
- Cas dégénéré sizes nulles → REJECTED.
- Property test ``hypothesis`` : invariants ``filled_size ≤ requested_size``,
  ``avg_price ∈ [min_level_price, max_level_price]`` sur le côté consommé.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from polycopy.executor.dtos import Orderbook, OrderbookLevel
from polycopy.executor.realistic_fill import simulate_fill
from polycopy.strategy.dtos import OrderApproved


def _order(side: Literal["BUY", "SELL"], size: float) -> OrderApproved:
    return OrderApproved(
        detected_trade_id=1,
        tx_hash="0xtest",
        condition_id="0xcond",
        asset_id="42",
        side=side,
        my_size=size,
        my_price=0.5,
    )


def _book(
    *,
    asks: list[tuple[str, str]] | None = None,
    bids: list[tuple[str, str]] | None = None,
) -> Orderbook:
    return Orderbook(
        asset_id="42",
        bids=[OrderbookLevel(price=Decimal(p), size=Decimal(s)) for p, s in (bids or [])],
        asks=[OrderbookLevel(price=Decimal(p), size=Decimal(s)) for p, s in (asks or [])],
        snapshot_at=datetime.now(tz=UTC),
    )


def test_buy_happy_path_weighted_avg() -> None:
    book = _book(asks=[("0.08", "50"), ("0.09", "60")])
    fill = simulate_fill(_order("BUY", 100.0), book, allow_partial=False)
    assert fill.status == "SIMULATED"
    assert fill.filled_size == 100.0
    # (50*0.08 + 50*0.09)/100 = 0.085
    assert fill.avg_fill_price == 0.085
    assert fill.depth_consumed_levels == 2
    assert fill.shortfall == 0.0


def test_buy_insufficient_liquidity_rejects_fok_strict() -> None:
    book = _book(asks=[("0.08", "50"), ("0.09", "60")])
    fill = simulate_fill(_order("BUY", 200.0), book, allow_partial=False)
    assert fill.status == "REJECTED"
    assert fill.reason == "insufficient_liquidity"
    assert fill.filled_size == 0.0
    assert fill.depth_consumed_shares == 110.0  # what we *would* have consumed
    assert fill.shortfall == 90.0
    assert fill.avg_fill_price is None


def test_buy_partial_book_allowed() -> None:
    book = _book(asks=[("0.08", "50"), ("0.09", "60")])
    fill = simulate_fill(_order("BUY", 200.0), book, allow_partial=True)
    assert fill.status == "SIMULATED"
    assert fill.filled_size == 110.0
    assert fill.depth_consumed_levels == 2
    assert fill.shortfall == 90.0
    assert fill.avg_fill_price == pytest.approx((50 * 0.08 + 60 * 0.09) / 110, abs=1e-9)


def test_sell_happy_path_consumes_bids() -> None:
    book = _book(bids=[("0.07", "100")])
    fill = simulate_fill(_order("SELL", 50.0), book, allow_partial=False)
    assert fill.status == "SIMULATED"
    assert fill.filled_size == 50.0
    assert fill.avg_fill_price == 0.07


def test_sell_consumes_best_bid_first() -> None:
    book = _book(bids=[("0.07", "30"), ("0.06", "100")])
    fill = simulate_fill(_order("SELL", 50.0), book, allow_partial=False)
    assert fill.status == "SIMULATED"
    # 30 @ 0.07 + 20 @ 0.06 = 2.10 + 1.20 = 3.30 / 50 = 0.066
    assert fill.avg_fill_price == pytest.approx(0.066, abs=1e-9)


def test_buy_empty_book_rejects_with_empty_book_reason() -> None:
    book = _book(asks=[])
    fill = simulate_fill(_order("BUY", 10.0), book, allow_partial=False)
    assert fill.status == "REJECTED"
    assert fill.reason == "empty_book"
    assert fill.shortfall == 10.0


def test_sell_empty_book_rejects_with_empty_book_reason() -> None:
    book = _book(bids=[])
    fill = simulate_fill(_order("SELL", 10.0), book, allow_partial=True)
    assert fill.status == "REJECTED"
    assert fill.reason == "empty_book"


def test_buy_decimal_precision_tiny_sizes() -> None:
    book = _book(asks=[("0.08", "1.0")])
    fill = simulate_fill(_order("BUY", 0.001), book, allow_partial=False)
    assert fill.status == "SIMULATED"
    assert fill.filled_size == 0.001
    assert fill.avg_fill_price == 0.08


def test_buy_zero_size_levels_treated_as_empty_book() -> None:
    book = _book(asks=[("0.08", "0"), ("0.09", "0")])
    fill = simulate_fill(_order("BUY", 10.0), book, allow_partial=True)
    assert fill.status == "REJECTED"
    assert fill.reason == "empty_book"


# --- Property tests --------------------------------------------------------


_LEVEL_STRAT = st.tuples(
    st.decimals(min_value="0.001", max_value="0.999", places=3, allow_nan=False),
    st.decimals(min_value="1", max_value="100000", places=2, allow_nan=False),
)


_REQUESTED = st.floats(
    min_value=0.5,
    max_value=50_000,
    allow_nan=False,
    allow_infinity=False,
)


@settings(max_examples=200, deadline=None)
@given(
    asks_raw=st.lists(_LEVEL_STRAT, min_size=1, max_size=20),
    requested_size=_REQUESTED,
)
def test_property_buy_filled_size_lte_requested(
    asks_raw: list[tuple[Decimal, Decimal]], requested_size: float
) -> None:
    asks = [OrderbookLevel(price=p, size=s) for p, s in asks_raw]
    book = Orderbook(
        asset_id="42",
        bids=[],
        asks=sorted(asks, key=lambda lvl: lvl.price),
        snapshot_at=datetime.now(tz=UTC),
    )
    fill = simulate_fill(_order("BUY", requested_size), book, allow_partial=True)
    assert fill.filled_size <= requested_size + 1e-6
    if fill.status == "SIMULATED":
        assert fill.avg_fill_price is not None
        prices = [float(a.price) for a in book.asks]
        # avg pondéré ∈ [min, max] des niveaux consommés
        assert min(prices) - 1e-9 <= fill.avg_fill_price <= max(prices) + 1e-9
