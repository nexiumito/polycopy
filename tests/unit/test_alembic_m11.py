"""Smoke test Alembic M11 : 0005 crée la table ``trade_latency_samples`` et
le downgrade la supprime proprement.
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


def test_m11_upgrade_creates_trade_latency_samples(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "m11_upgrade.db"
    cfg = _make_config(db_path, project_root)
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "trade_latency_samples" in tables
        cols = {c["name"] for c in inspector.get_columns("trade_latency_samples")}
        assert {"id", "trade_id", "stage_name", "duration_ms", "timestamp"} <= cols
        idx_names = {i["name"] for i in inspector.get_indexes("trade_latency_samples")}
        assert "ix_trade_latency_samples_stage_ts" in idx_names
        assert "ix_trade_latency_samples_trade_id" in idx_names
    finally:
        engine.dispose()


def test_m11_downgrade_drops_table_cleanly(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "m11_downgrade.db"
    cfg = _make_config(db_path, project_root)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0004_m8_dry_run_realistic")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "trade_latency_samples" not in tables
        # Les tables M8 et antérieures doivent rester intactes.
        assert "my_orders" in tables
        assert "my_positions" in tables
    finally:
        engine.dispose()
