"""Tests des DTOs du Strategy Engine (parsing fixture Gamma réelle)."""

from datetime import UTC, datetime
from typing import Any

import pytest

from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.strategy.dtos import (
    FilterResult,
    MarketMetadata,
    OrderApproved,
    PipelineContext,
)


def test_market_metadata_parses_real_gamma_payload(
    sample_gamma_market: dict[str, Any],
) -> None:
    market = MarketMetadata.model_validate(sample_gamma_market)
    assert market.condition_id.startswith("0x")
    assert market.market_id == sample_gamma_market["id"]
    assert market.active is True
    assert market.closed is False
    assert market.liquidity_clob is not None and market.liquidity_clob > 0
    # clobTokenIds est une string '["123","456"]' côté API ; doit devenir une liste.
    assert isinstance(market.clob_token_ids, list)
    assert len(market.clob_token_ids) >= 1
    assert all(isinstance(t, str) for t in market.clob_token_ids)
    # outcomes idem
    assert market.outcomes == ["Yes", "No"]


def test_market_metadata_handles_null_json_strings() -> None:
    raw = {
        "id": "1",
        "conditionId": "0xc",
        "active": True,
        "closed": False,
        "archived": False,
        "clobTokenIds": None,
        "outcomes": None,
    }
    market = MarketMetadata.model_validate(raw)
    assert market.clob_token_ids == []
    assert market.outcomes == []


def test_market_metadata_parses_iso_end_date() -> None:
    raw = {
        "id": "1",
        "conditionId": "0xc",
        "active": True,
        "closed": False,
        "archived": False,
        "endDate": "2026-06-30T00:00:00Z",
    }
    market = MarketMetadata.model_validate(raw)
    assert market.end_date == datetime(2026, 6, 30, 0, 0, 0, tzinfo=UTC)


def test_order_approved_is_frozen() -> None:
    from pydantic import ValidationError

    event = OrderApproved(
        detected_trade_id=1,
        tx_hash="0xtx",
        condition_id="0xc",
        asset_id="123",
        side="BUY",
        my_size=10.0,
        my_price=0.5,
    )
    with pytest.raises(ValidationError):
        event.my_size = 99.0  # type: ignore[misc]


def test_pipeline_context_audit_dict_is_serializable(
    sample_gamma_market: dict[str, Any],
) -> None:
    trade = DetectedTradeDTO(
        tx_hash="0xtx",
        target_wallet="0xw",
        condition_id="0xc",
        asset_id="123",
        side="BUY",
        size=10.0,
        usdc_size=5.0,
        price=0.5,
        timestamp=datetime.now(tz=UTC),
        raw_json={},
    )
    ctx = PipelineContext(trade=trade)
    ctx.market = MarketMetadata.model_validate(sample_gamma_market)
    ctx.midpoint = 0.51
    ctx.my_size = 1.5
    ctx.slippage_pct = 0.4
    ctx.record_filter("MarketFilter", FilterResult(passed=True))
    audit = ctx.to_audit_dict()
    import json

    serialized = json.dumps(audit, default=str)  # tolère datetime
    assert "MarketFilter" in serialized
    assert audit["midpoint"] == 0.51
    assert audit["filter_trace"][0]["passed"] is True
