"""Smoke test Alembic M12 : 0006 crée la table ``trader_daily_pnl`` et le
downgrade la supprime proprement. Les tables M5/M11 restent intactes.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command


def _make_config(db_path: Path, project_root: Path) -> Config:
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_m12_upgrade_creates_trader_daily_pnl(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "m12_upgrade.db"
    cfg = _make_config(db_path, project_root)
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "trader_daily_pnl" in tables
        cols = {c["name"] for c in inspector.get_columns("trader_daily_pnl")}
        assert {
            "id",
            "wallet_address",
            "date",
            "equity_usdc",
            "realized_pnl_day",
            "unrealized_pnl_day",
            "positions_count",
            "snapshotted_at",
        } <= cols
        idx_names = {i["name"] for i in inspector.get_indexes("trader_daily_pnl")}
        assert "ix_trader_daily_pnl_wallet_address" in idx_names
        assert "ix_trader_daily_pnl_date" in idx_names
        assert "ix_trader_daily_pnl_wallet_date" in idx_names
        # Contrainte unique (wallet, date).
        uniques = inspector.get_unique_constraints("trader_daily_pnl")
        assert any(set(u.get("column_names", [])) == {"wallet_address", "date"} for u in uniques), (
            f"expected uq(wallet_address,date) in {uniques}"
        )
    finally:
        engine.dispose()


def test_m12_downgrade_drops_table_cleanly(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "m12_downgrade.db"
    cfg = _make_config(db_path, project_root)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0005_m11_latency_samples")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "trader_daily_pnl" not in tables
        # Non-régression : tables M5/M11 antérieures intactes.
        assert "trader_scores" in tables
        assert "trader_events" in tables
        assert "trade_latency_samples" in tables
        assert "target_traders" in tables
    finally:
        engine.dispose()


def test_m12_re_upgrade_is_idempotent(tmp_path: Path) -> None:
    """upgrade → downgrade → upgrade produit la même structure sans crash."""
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "m12_idempotent.db"
    cfg = _make_config(db_path, project_root)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0005_m11_latency_samples")
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        assert "trader_daily_pnl" in set(inspector.get_table_names())
    finally:
        engine.dispose()
