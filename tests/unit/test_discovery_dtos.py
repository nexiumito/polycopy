"""Tests DTOs discovery M5 — validation + propriétés computed."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from polycopy.discovery.dtos import (
    CandidateWallet,
    DiscoveryDecision,
    GlobalTrade,
    HolderEntry,
    RawPosition,
    ScoringResult,
    TraderMetrics,
)


def test_holder_entry_parses_from_data_api_shape() -> None:
    raw = {
        "proxyWallet": "0xABC",
        "amount": 1234.5,
        "outcomeIndex": 1,
        "pseudonym": "alpha",
        "name": "alpha-name",
        "profileImage": "https://cdn.example/x.png",
    }
    h = HolderEntry.model_validate(raw)
    assert h.proxy_wallet == "0xABC"
    assert h.amount == 1234.5
    assert h.outcome_index == 1
    assert h.pseudonym == "alpha"


def test_global_trade_usdc_size_is_computed_client_side() -> None:
    """§14.5 #2 : l'API ne renvoie pas `usdcSize`, on le recalcule."""
    raw = {
        "proxyWallet": "0xABC",
        "asset": "1",
        "conditionId": "0xcond",
        "side": "BUY",
        "size": 500,
        "price": 0.34,
        "timestamp": 1776504298,
        "transactionHash": "0xtx",
        "title": "market",
        "slug": "market",
    }
    t = GlobalTrade.model_validate(raw)
    assert t.usdc_size == pytest.approx(170.0)


@pytest.mark.parametrize(
    ("redeemable", "current_value", "realized_pnl", "expected"),
    [
        (True, 100.0, 0.0, True),
        (False, 0.0, 0.0, True),
        (False, 100.0, 5.0, True),
        (False, 100.0, 0.0, False),
    ],
)
def test_raw_position_is_resolved_heuristic(
    redeemable: bool,
    current_value: float,
    realized_pnl: float,
    expected: bool,
) -> None:
    """§14.5 #4 : 3 indicateurs combinés pour 'position résolue'."""
    p = RawPosition(
        conditionId="0xc",
        asset="1",
        size=10,
        avgPrice=0.5,
        initialValue=100.0,
        currentValue=current_value,
        cashPnl=0.0,
        realizedPnl=realized_pnl,
        totalBought=100.0,
        redeemable=redeemable,
    )
    assert p.is_resolved is expected


def test_candidate_wallet_frozen() -> None:
    c = CandidateWallet(
        wallet_address="0xabc",
        discovered_via="holders",
        initial_signal=1.5,
    )
    with pytest.raises(ValidationError):  # pydantic frozen
        c.wallet_address = "0xother"  # type: ignore[misc]


def test_scoring_result_bounded() -> None:
    from datetime import UTC, datetime

    with pytest.raises(ValidationError):
        ScoringResult(
            wallet_address="0xa",
            score=1.5,
            scoring_version="v1",
            low_confidence=False,
            metrics=TraderMetrics(
                wallet_address="0xa",
                fetched_at=datetime.now(tz=UTC),
            ),
            cycle_at=datetime.now(tz=UTC),
        )


def test_discovery_decision_default_metadata_empty() -> None:
    d = DiscoveryDecision(
        wallet_address="0xa",
        decision="keep",
        from_status="active",
        to_status="active",
        scoring_version="v1",
        reason="ok",
    )
    assert d.event_metadata == {}
