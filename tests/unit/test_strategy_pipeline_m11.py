"""Tests M11 sur ``SlippageChecker`` : lookup WS + fallback HTTP (§9.3.B)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from polycopy.config import Settings
from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.strategy.dtos import PipelineContext
from polycopy.strategy.pipeline import SlippageChecker


def _make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {"target_wallets": [], "max_slippage_pct": 5.0}
    base.update(overrides)
    return Settings(**base)


def _make_trade() -> DetectedTradeDTO:
    from datetime import UTC, datetime

    return DetectedTradeDTO(
        tx_hash="0xtx",
        target_wallet="0xw",
        condition_id="0xc",
        asset_id="tok_a",
        side="BUY",
        size=10.0,
        usdc_size=1.0,
        price=0.10,
        timestamp=datetime.now(tz=UTC),
        raw_json={},
    )


async def test_slippage_checker_uses_ws_cache_when_available() -> None:
    """WS renvoie mid=0.10 → HTTP pas appelé."""
    settings = _make_settings()
    ws_client = MagicMock()
    ws_client.subscribe = AsyncMock()
    ws_client.get_mid_price = AsyncMock(return_value=0.10)
    clob_client = MagicMock()
    clob_client.get_midpoint = AsyncMock(return_value=None)
    trade = _make_trade()
    ctx = PipelineContext(trade=trade)
    checker = SlippageChecker(clob_client, settings, ws_client=ws_client)
    result = await checker.check(ctx)
    assert result.passed
    assert ctx.midpoint == pytest.approx(0.10)
    ws_client.subscribe.assert_awaited_once_with("tok_a")
    ws_client.get_mid_price.assert_awaited_once_with("tok_a")
    clob_client.get_midpoint.assert_not_called()


async def test_slippage_checker_fallback_to_http_when_ws_returns_none() -> None:
    """WS renvoie None → HTTP appelé, résultat utilisé."""
    settings = _make_settings()
    ws_client = MagicMock()
    ws_client.subscribe = AsyncMock()
    ws_client.get_mid_price = AsyncMock(return_value=None)
    clob_client = MagicMock()
    clob_client.get_midpoint = AsyncMock(return_value=0.105)
    trade = _make_trade()
    ctx = PipelineContext(trade=trade)
    checker = SlippageChecker(clob_client, settings, ws_client=ws_client)
    result = await checker.check(ctx)
    assert result.passed
    assert ctx.midpoint == pytest.approx(0.105)
    clob_client.get_midpoint.assert_awaited_once_with("tok_a")


async def test_slippage_checker_fallback_when_ws_feature_flag_disabled() -> None:
    """Flag ``strategy_clob_ws_enabled=False`` → WS ignoré même si fourni."""
    settings = _make_settings(strategy_clob_ws_enabled=False)
    ws_client = MagicMock()
    ws_client.subscribe = AsyncMock()
    ws_client.get_mid_price = AsyncMock(return_value=0.99)
    clob_client = MagicMock()
    clob_client.get_midpoint = AsyncMock(return_value=0.10)
    trade = _make_trade()
    ctx = PipelineContext(trade=trade)
    checker = SlippageChecker(clob_client, settings, ws_client=ws_client)
    result = await checker.check(ctx)
    assert result.passed
    ws_client.subscribe.assert_not_called()
    ws_client.get_mid_price.assert_not_called()
    clob_client.get_midpoint.assert_awaited_once()


async def test_slippage_checker_no_ws_client_is_m2_behaviour() -> None:
    """``ws_client=None`` (default) → comportement M2 strict (HTTP direct)."""
    settings = _make_settings()
    clob_client = MagicMock()
    clob_client.get_midpoint = AsyncMock(return_value=0.10)
    trade = _make_trade()
    ctx = PipelineContext(trade=trade)
    checker = SlippageChecker(clob_client, settings)
    result = await checker.check(ctx)
    assert result.passed
    clob_client.get_midpoint.assert_awaited_once()
