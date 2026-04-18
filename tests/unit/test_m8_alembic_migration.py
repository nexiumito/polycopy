"""Tests M8 §9.8 — migration ``0004_m8_dry_run_realistic``."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command


def _make_config(db_path: Path) -> Config:
    project_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_upgrade_to_0004_adds_columns_and_unique_triple(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path / "m8.db")
    command.upgrade(cfg, "0004_m8_dry_run_realistic")

    engine = create_engine(f"sqlite:///{tmp_path / 'm8.db'}")
    try:
        inspector = inspect(engine)
        order_cols = {c["name"] for c in inspector.get_columns("my_orders")}
        assert "realistic_fill" in order_cols
        position_cols = {c["name"] for c in inspector.get_columns("my_positions")}
        assert {"simulated", "realized_pnl"} <= position_cols
        # Index présent
        idx_names = {i["name"] for i in inspector.get_indexes("my_positions")}
        assert "ix_my_positions_simulated_open" in idx_names
        # Contrainte unique triple
        uniques = inspector.get_unique_constraints("my_positions")
        triple = [
            uc
            for uc in uniques
            if set(uc["column_names"]) == {"condition_id", "asset_id", "simulated"}
        ]
        assert triple, "uq_my_positions_condition_asset_simulated missing"
        # L'ancienne contrainte a disparu
        legacy = [uc for uc in uniques if set(uc["column_names"]) == {"condition_id", "asset_id"}]
        assert not legacy
    finally:
        engine.dispose()


def test_unique_triple_allows_real_and_virtual_coexistence(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path / "m8b.db")
    command.upgrade(cfg, "0004_m8_dry_run_realistic")
    url = f"sqlite:///{tmp_path / 'm8b.db'}"
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO my_positions "
                    "(condition_id, asset_id, size, avg_price, opened_at, simulated) "
                    "VALUES ('0xC', 'A', 1.0, 0.5, '2026-04-18', 0)"
                ),
            )
            conn.execute(
                text(
                    "INSERT INTO my_positions "
                    "(condition_id, asset_id, size, avg_price, opened_at, simulated) "
                    "VALUES ('0xC', 'A', 2.0, 0.3, '2026-04-18', 1)"
                ),
            )
        # 2ᵉ insert virtuel sur même triple → IntegrityError
        with engine.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO my_positions "
                    "(condition_id, asset_id, size, avg_price, opened_at, simulated) "
                    "VALUES ('0xC', 'A', 3.0, 0.4, '2026-04-18', 1)"
                ),
            )
    finally:
        engine.dispose()


def test_downgrade_from_0004_restores_pair_constraint(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path / "m8c.db")
    command.upgrade(cfg, "0004_m8_dry_run_realistic")
    command.downgrade(cfg, "0003_m5_discovery")
    engine = create_engine(f"sqlite:///{tmp_path / 'm8c.db'}")
    try:
        inspector = inspect(engine)
        position_cols = {c["name"] for c in inspector.get_columns("my_positions")}
        assert "simulated" not in position_cols
        assert "realized_pnl" not in position_cols
        order_cols = {c["name"] for c in inspector.get_columns("my_orders")}
        assert "realistic_fill" not in order_cols
        uniques = inspector.get_unique_constraints("my_positions")
        pair = [uc for uc in uniques if set(uc["column_names"]) == {"condition_id", "asset_id"}]
        assert pair
    finally:
        engine.dispose()
