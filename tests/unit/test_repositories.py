"""Tests des repositories storage."""

from datetime import UTC, datetime, timedelta

from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.repositories import (
    DetectedTradeRepository,
    TargetTraderRepository,
)


def _make_dto(tx_hash: str, wallet: str, ts: datetime) -> DetectedTradeDTO:
    return DetectedTradeDTO(
        tx_hash=tx_hash,
        target_wallet=wallet,
        condition_id="0xcond",
        asset_id="123",
        side="BUY",
        size=10.0,
        usdc_size=5.0,
        price=0.5,
        timestamp=ts,
        outcome="Yes",
        slug="market-slug",
        raw_json={"tx_hash": tx_hash},
    )


def _to_utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


async def test_target_trader_upsert_idempotent(
    target_trader_repo: TargetTraderRepository,
) -> None:
    first = await target_trader_repo.upsert("0xABC", label="alice")
    second = await target_trader_repo.upsert("0xabc", label="alice2")
    actives = await target_trader_repo.list_active()
    assert len(actives) == 1
    assert first.id == second.id
    assert second.wallet_address == "0xabc"
    assert second.label == "alice2"
    assert second.active is True


async def test_detected_trade_insert_dedup(
    detected_trade_repo: DetectedTradeRepository,
) -> None:
    ts = datetime.now(tz=UTC)
    dto = _make_dto("0xtx1", "0xWALLET", ts)
    assert await detected_trade_repo.insert_if_new(dto) is True
    assert await detected_trade_repo.insert_if_new(dto) is False
    assert await detected_trade_repo.count_for_wallet("0xwallet") == 1


async def test_get_latest_timestamp_returns_max(
    detected_trade_repo: DetectedTradeRepository,
) -> None:
    base = datetime.now(tz=UTC).replace(microsecond=0)
    for i in range(3):
        await detected_trade_repo.insert_if_new(
            _make_dto(f"0xtx{i}", "0xwallet", base + timedelta(seconds=i)),
        )
    latest = await detected_trade_repo.get_latest_timestamp("0xwallet")
    assert latest is not None
    assert int(_to_utc(latest).timestamp()) == int((base + timedelta(seconds=2)).timestamp())


async def test_get_latest_timestamp_unknown_wallet_is_none(
    detected_trade_repo: DetectedTradeRepository,
) -> None:
    assert await detected_trade_repo.get_latest_timestamp("0xunknown") is None
