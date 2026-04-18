"""Populate une DB SQLite avec des données de démo pour les screenshots.

**Idempotent** : nettoie les tables avant insert pour reproductibilité.
**Reproductible** : timestamps fixes (`SEED_REFERENCE_DT`) + random.seed(42).

Usage :
    python scripts/seed_demo_db.py [--db-url sqlite+aiosqlite:///polycopy.db]

Hors-scope M9 : pas de migration auto, pas de Telegram sample, pas de
métriques discovery (le screenshot Traders dérive directement des
target_traders + scores).
"""

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete

from polycopy.config import settings
from polycopy.storage.engine import create_engine_and_session
from polycopy.storage.init_db import init_db
from polycopy.storage.models import (
    DetectedTrade,
    MyOrder,
    MyPosition,
    PnlSnapshot,
    StrategyDecision,
    TargetTrader,
    TraderEvent,
    TraderScore,
)

log = structlog.get_logger(__name__)

# Timestamps fixes : 18 avril 2026 12:00 UTC. Déterministes pour les screenshots.
SEED_REFERENCE_DT = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
SEED_RANDOM = random.Random(42)  # noqa: S311 — déterminisme demo, pas de cryptographie

DEMO_WALLETS: list[tuple[str, str, float]] = [
    ("0x1111111111111111111111111111111111111111", "Fredi9999", 0.82),
    ("0x2222222222222222222222222222222222222222", "WhalePoly", 0.74),
    ("0x3333333333333333333333333333333333333333", "MacroBetter", 0.68),
    ("0x4444444444444444444444444444444444444444", "ElectionDude", 0.59),
]

DEMO_MARKETS: list[tuple[str, str, str]] = [
    (
        "0xaaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111",
        "1111111111111111111111111111111111111111111111111111111111111111",
        "will-bitcoin-reach-150k-by-2026",
    ),
    (
        "0xbbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222",
        "2222222222222222222222222222222222222222222222222222222222222222",
        "will-fed-cut-rates-in-may-2026",
    ),
    (
        "0xcccc3333cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333",
        "3333333333333333333333333333333333333333333333333333333333333333",
        "next-uk-pm-by-end-2026",
    ),
]


async def _clean_tables(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Vide les tables seedées (idempotence stricte)."""
    async with session_factory() as session:
        for tbl in (
            PnlSnapshot,
            MyPosition,
            MyOrder,
            StrategyDecision,
            DetectedTrade,
            TraderScore,
            TraderEvent,
            TargetTrader,
        ):
            await session.execute(delete(tbl))
        await session.commit()
    log.info("seed_tables_cleared")


async def _seed_traders(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        for i, (addr, label, score) in enumerate(DEMO_WALLETS):
            trader = TargetTrader(
                wallet_address=addr,
                label=label,
                score=score,
                active=i < 3,
                added_at=SEED_REFERENCE_DT - timedelta(days=14 - i),
                status="active" if i < 3 else "shadow",
                pinned=(i == 0),
                consecutive_low_score_cycles=0,
                discovered_at=SEED_REFERENCE_DT - timedelta(days=14 - i),
                promoted_at=SEED_REFERENCE_DT - timedelta(days=10) if i < 3 else None,
                last_scored_at=SEED_REFERENCE_DT - timedelta(hours=2),
                scoring_version="v1",
            )
            session.add(trader)
        await session.commit()
    log.info("seed_traders_inserted", count=len(DEMO_WALLETS))


async def _seed_trades_and_orders(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        for i in range(10):
            wallet, _, _ = DEMO_WALLETS[i % len(DEMO_WALLETS)]
            cond, asset, slug = DEMO_MARKETS[i % len(DEMO_MARKETS)]
            tx = f"0x{i:064x}"
            ts = SEED_REFERENCE_DT - timedelta(hours=24 - i * 2)
            price = round(0.1 + SEED_RANDOM.random() * 0.7, 4)
            size = round(50 + SEED_RANDOM.random() * 200, 2)
            trade = DetectedTrade(
                tx_hash=tx,
                target_wallet=wallet,
                condition_id=cond,
                asset_id=asset,
                side="BUY" if i % 2 == 0 else "SELL",
                size=size,
                usdc_size=round(size * price, 2),
                price=price,
                timestamp=ts,
                outcome="Yes" if i % 2 == 0 else "No",
                slug=slug,
                raw_json={"demo": True, "i": i},
            )
            session.add(trade)
        await session.commit()

    # 3 ordres demo (statuts variés).
    async with session_factory() as session:
        for i in range(3):
            cond, asset, _ = DEMO_MARKETS[i]
            order = MyOrder(
                source_tx_hash=f"0x{i:064x}",
                clob_order_id=f"clob-demo-{i}",
                condition_id=cond,
                asset_id=asset,
                side="BUY",
                size=10.0 + i,
                price=0.42 + i * 0.05,
                tick_size=0.01,
                neg_risk=False,
                order_type="FOK",
                status=("FILLED", "REJECTED", "SIMULATED")[i],
                taking_amount="4200000",
                making_amount="10000000",
                transaction_hashes=[f"0x{i + 100:064x}"],
                error_msg=None if i != 1 else "demo_slippage_exceeded",
                simulated=(i == 2),
                realistic_fill=False,
                sent_at=SEED_REFERENCE_DT - timedelta(hours=8 - i),
                filled_at=(
                    SEED_REFERENCE_DT - timedelta(hours=8 - i, minutes=-1) if i == 0 else None
                ),
            )
            session.add(order)
        await session.commit()
    log.info("seed_trades_and_orders_inserted", trades=10, orders=3)


async def _seed_positions(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        for i, (cond, asset, _) in enumerate(DEMO_MARKETS[:2]):
            pos = MyPosition(
                condition_id=cond,
                asset_id=asset,
                size=12.5 - i * 4,
                avg_price=0.35 + i * 0.1,
                opened_at=SEED_REFERENCE_DT - timedelta(hours=6 - i),
                closed_at=None,
                simulated=False,
                realized_pnl=None,
            )
            session.add(pos)
        await session.commit()
    log.info("seed_positions_inserted", count=2)


async def _seed_pnl_snapshots(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        # 5 snapshots à H-4, H-3, H-2, H-1, now : courbe légèrement haussière.
        for i in range(5):
            ts = SEED_REFERENCE_DT - timedelta(hours=4 - i)
            total = 1000.0 + i * 12.5 + SEED_RANDOM.uniform(-3, 3)
            realized = 18.3 + i * 1.5
            unrealized = 2.7 + SEED_RANDOM.uniform(-1, 1)
            drawdown = max(0.0, 5.0 - i * 0.8)
            snap = PnlSnapshot(
                timestamp=ts,
                total_usdc=total,
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                drawdown_pct=drawdown,
                open_positions_count=2,
                cash_pnl_total=realized + unrealized,
                is_dry_run=False,
            )
            session.add(snap)
        await session.commit()
    log.info("seed_pnl_snapshots_inserted", count=5)


async def _seed_trader_scores(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        for addr, _, score in DEMO_WALLETS:
            ts_score = TraderScore(
                wallet_address=addr,
                score=score,
                scoring_version="v1",
                computed_at=SEED_REFERENCE_DT - timedelta(hours=2),
                win_rate=0.55 + SEED_RANDOM.random() * 0.20,
                roi_realized_pct=8.0 + SEED_RANDOM.random() * 30.0,
                diversity_index=0.45 + SEED_RANDOM.random() * 0.30,
                volume_usdc=10_000.0 + SEED_RANDOM.random() * 90_000.0,
                closed_markets_count=15 + SEED_RANDOM.randint(0, 80),
                low_confidence=False,
            )
            session.add(ts_score)
        await session.commit()
    log.info("seed_trader_scores_inserted", count=len(DEMO_WALLETS))


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed demo DB pour screenshots M9")
    parser.add_argument("--db-url", default=None, help="Override DATABASE_URL.")
    args = parser.parse_args()

    db_url = args.db_url or settings.database_url
    log.info("seed_demo_db_starting", db_url=db_url)

    engine, session_factory = create_engine_and_session(db_url)
    try:
        await init_db(engine, session_factory, [w for w, _, _ in DEMO_WALLETS[:1]])
        await _clean_tables(session_factory)
        await _seed_traders(session_factory)
        await _seed_trades_and_orders(session_factory)
        await _seed_positions(session_factory)
        await _seed_pnl_snapshots(session_factory)
        await _seed_trader_scores(session_factory)
        log.info("seed_demo_db_done")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
