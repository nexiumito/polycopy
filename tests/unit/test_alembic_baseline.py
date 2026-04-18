"""Smoke test Alembic : ``upgrade head`` crée bien toutes les tables."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command


def _make_config(db_path: Path, project_root: Path) -> Config:
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_alembic_upgrade_head_creates_all_tables(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "baseline.db"
    cfg = _make_config(db_path, project_root)
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        expected = {
            "target_traders",
            "detected_trades",
            "strategy_decisions",
            "my_orders",
            "my_positions",
            "pnl_snapshots",
        }
        assert expected <= tables
        # Version est marquée.
        assert "alembic_version" in tables
        # PnlSnapshot contient bien les 3 nouvelles colonnes M4.
        cols = {c["name"] for c in inspector.get_columns("pnl_snapshots")}
        assert {"open_positions_count", "cash_pnl_total", "is_dry_run"} <= cols
    finally:
        engine.dispose()


def test_init_db_autostamps_legacy_m3_db(tmp_path: Path) -> None:
    """Scénario M3→M4 automatique : DB au schéma baseline (pré-M4), sans
    ``alembic_version``, puis appel de ``_run_alembic_upgrade`` qui doit
    détecter l'état "legacy", stamp la baseline, et upgrader à head.
    """
    from polycopy.storage.init_db import _run_alembic_upgrade

    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "legacy_m3.db"

    # 1) Applique la baseline M3 via Alembic (crée le schéma pré-M4).
    cfg = _make_config(db_path, project_root)
    command.upgrade(cfg, "0001_baseline_m3")

    # 2) Simule une DB legacy : supprime ``alembic_version`` pour que l'on
    #    se retrouve dans l'état exact d'un user M3 pré-Alembic.
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE alembic_version")
    finally:
        engine.dispose()

    # 3) init_db doit stamp puis upgrader.
    _run_alembic_upgrade(f"sqlite+aiosqlite:///{db_path}")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "alembic_version" in tables
        cols = {c["name"] for c in inspector.get_columns("pnl_snapshots")}
        assert {"open_positions_count", "cash_pnl_total", "is_dry_run"} <= cols
    finally:
        engine.dispose()
