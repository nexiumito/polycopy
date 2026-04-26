"""Tests M17 MD.3 — migration Alembic 0010 (audit C-003 + H-005 schema).

Couvre :
- Schéma post-upgrade : `pnl_snapshots.execution_mode` + CHECK +
  `trader_events.wallet_address` nullable.
- Backfill SQL `UPDATE` : is_dry_run=1 → 'dry_run', is_dry_run=0 → 'live'.
- Idempotence upgrade → downgrade → re-upgrade.
- Downgrade safeguard : refuse si wallet_address NULL existe.
- Repository.get_max_total_usdc segregation par mode.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def _make_config(db_path: Path, project_root: Path) -> Config:
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_migration_0010_upgrade_creates_execution_mode_column(tmp_path: Path) -> None:
    """`alembic upgrade head` crée la colonne + CHECK constraint."""
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "m17.db"
    cfg = _make_config(db_path, project_root)
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        cols = {c["name"]: c for c in inspector.get_columns("pnl_snapshots")}
        assert "execution_mode" in cols
        assert cols["execution_mode"]["nullable"] is False
        # trader_events.wallet_address relâchée NULL post-migration.
        te_cols = {c["name"]: c for c in inspector.get_columns("trader_events")}
        assert te_cols["wallet_address"]["nullable"] is True
    finally:
        engine.dispose()


def test_migration_0010_backfill_correct(tmp_path: Path) -> None:
    """Backfill SQL : is_dry_run=1 → 'dry_run', is_dry_run=0 → 'live'."""
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "m17_backfill.db"
    cfg = _make_config(db_path, project_root)

    # Upgrade jusqu'à 0009 pour insérer des rows pré-M17.
    command.upgrade(cfg, "0009_m15_anti_toxic_lifecycle")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            for is_dry in (0, 1, 1, 0, 1):
                conn.execute(
                    text(
                        "INSERT INTO pnl_snapshots "
                        "(timestamp, total_usdc, realized_pnl, unrealized_pnl, "
                        "drawdown_pct, open_positions_count, is_dry_run) "
                        "VALUES (CURRENT_TIMESTAMP, 1000.0, 0.0, 0.0, 0.0, 0, :d)",
                    ),
                    {"d": is_dry},
                )
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT is_dry_run, execution_mode FROM pnl_snapshots ORDER BY id"),
            ).all()
        assert rows == [
            (0, "live"),
            (1, "dry_run"),
            (1, "dry_run"),
            (0, "live"),
            (1, "dry_run"),
        ]
    finally:
        engine.dispose()


def test_migration_0010_downgrade_then_reupgrade_idempotent(tmp_path: Path) -> None:
    """upgrade → downgrade -1 → upgrade : DB cohérente, pas de drift."""
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "m17_idempotent.db"
    cfg = _make_config(db_path, project_root)

    command.upgrade(cfg, "0009_m15_anti_toxic_lifecycle")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO pnl_snapshots "
                    "(timestamp, total_usdc, realized_pnl, unrealized_pnl, "
                    "drawdown_pct, open_positions_count, is_dry_run) "
                    "VALUES (CURRENT_TIMESTAMP, 500.0, 0.0, 0.0, 0.0, 0, 1)",
                ),
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("pnl_snapshots")}
        assert "execution_mode" not in cols
        assert "is_dry_run" in cols  # rétrocompat préservée
    finally:
        engine.dispose()

    # Re-upgrade : backfill ré-appliqué, pas de drift.
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT is_dry_run, execution_mode FROM pnl_snapshots"),
            ).all()
        assert rows == [(1, "dry_run")]
    finally:
        engine.dispose()


def test_migration_0010_downgrade_refuses_if_wallet_null_present(tmp_path: Path) -> None:
    """Safeguard : downgrade refuse si trader_events a des rows wallet_address=NULL."""
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "m17_downgrade_refuse.db"
    cfg = _make_config(db_path, project_root)

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO trader_events (event_type, at, wallet_address) "
                    "VALUES ('kill_switch', CURRENT_TIMESTAMP, NULL)",
                ),
            )
    finally:
        engine.dispose()

    with pytest.raises(RuntimeError, match="system-level"):
        command.downgrade(cfg, "-1")


@pytest.mark.asyncio
async def test_get_max_total_usdc_segregates_by_mode(tmp_path: Path) -> None:
    """`get_max_total_usdc(execution_mode='dry_run')` ignore les snapshots live."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from polycopy.storage.dtos import PnlSnapshotDTO
    from polycopy.storage.repositories import PnlSnapshotRepository

    project_root = Path(__file__).resolve().parents[2]  # noqa: ASYNC240
    db_path = tmp_path / "m17_segregate.db"
    cfg = _make_config(db_path, project_root)
    command.upgrade(cfg, "head")

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    sf = async_sessionmaker(engine, expire_on_commit=False)
    try:
        repo = PnlSnapshotRepository(sf)
        # SIMULATION snapshot $50000, DRY_RUN $1000, LIVE $5000.
        await repo.insert(
            PnlSnapshotDTO(
                total_usdc=50000.0,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                drawdown_pct=0.0,
                open_positions_count=0,
                cash_pnl_total=None,
                is_dry_run=True,
                execution_mode="simulation",
            ),
        )
        await repo.insert(
            PnlSnapshotDTO(
                total_usdc=1000.0,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                drawdown_pct=0.0,
                open_positions_count=0,
                cash_pnl_total=None,
                is_dry_run=True,
                execution_mode="dry_run",
            ),
        )
        await repo.insert(
            PnlSnapshotDTO(
                total_usdc=5000.0,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                drawdown_pct=0.0,
                open_positions_count=0,
                cash_pnl_total=None,
                is_dry_run=False,
                execution_mode="live",
            ),
        )
        # Avant MD.3 : un get sur is_dry_run=True aurait retourné 50000
        # (pollution SIM+DRY). Avec MD.3 : segregation stricte.
        sim_max = await repo.get_max_total_usdc(execution_mode="simulation")
        dry_max = await repo.get_max_total_usdc(execution_mode="dry_run")
        live_max = await repo.get_max_total_usdc(execution_mode="live")
        assert sim_max == 50000.0
        assert dry_max == 1000.0
        assert live_max == 5000.0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_pnl_snapshot_writes_execution_mode(tmp_path: Path) -> None:
    """Le DTO sérialise execution_mode en DB, le repo lit la valeur correcte."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from polycopy.storage.dtos import PnlSnapshotDTO
    from polycopy.storage.repositories import PnlSnapshotRepository

    project_root = Path(__file__).resolve().parents[2]  # noqa: ASYNC240
    db_path = tmp_path / "m17_writes_mode.db"
    cfg = _make_config(db_path, project_root)
    command.upgrade(cfg, "head")

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    sf = async_sessionmaker(engine, expire_on_commit=False)
    try:
        repo = PnlSnapshotRepository(sf)
        await repo.insert(
            PnlSnapshotDTO(
                total_usdc=2000.0,
                realized_pnl=10.0,
                unrealized_pnl=5.0,
                drawdown_pct=0.0,
                open_positions_count=2,
                cash_pnl_total=None,
                is_dry_run=True,
                execution_mode="dry_run",
            ),
        )
        latest = await repo.get_latest(execution_mode="dry_run")
        assert latest is not None
        assert latest.execution_mode == "dry_run"
        assert latest.is_dry_run is True
    finally:
        await engine.dispose()
