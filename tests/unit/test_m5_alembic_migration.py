"""Tests de la migration Alembic 0003 (schéma discovery M5)."""

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


def test_upgrade_head_creates_m5_tables_and_columns(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "m5.db"
    cfg = _make_config(db_path, project_root)
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        # 1) Les 2 nouvelles tables existent.
        tables = set(inspector.get_table_names())
        assert {"trader_scores", "trader_events"} <= tables

        # 2) target_traders a les 7 nouvelles colonnes M5.
        tt_cols = {c["name"] for c in inspector.get_columns("target_traders")}
        expected = {
            "status",
            "pinned",
            "consecutive_low_score_cycles",
            "discovered_at",
            "promoted_at",
            "last_scored_at",
            "scoring_version",
        }
        assert expected <= tt_cols

        # 3) trader_scores a les 7 colonnes attendues.
        ts_cols = {c["name"] for c in inspector.get_columns("trader_scores")}
        assert {
            "target_trader_id",
            "wallet_address",
            "score",
            "scoring_version",
            "cycle_at",
            "low_confidence",
            "metrics_snapshot",
        } <= ts_cols

        # 4) trader_events a les 9 colonnes attendues (metadata = `event_metadata`).
        te_cols = {c["name"] for c in inspector.get_columns("trader_events")}
        assert {
            "wallet_address",
            "event_type",
            "at",
            "from_status",
            "to_status",
            "score_at_event",
            "scoring_version",
            "reason",
            "event_metadata",
        } <= te_cols

        # 5) Index composés indispensables pour perf dashboard.
        ts_indexes = {ix["name"] for ix in inspector.get_indexes("trader_scores")}
        te_indexes = {ix["name"] for ix in inspector.get_indexes("trader_events")}
        assert "ix_trader_scores_wallet_cycle" in ts_indexes
        assert "ix_trader_events_wallet_at" in te_indexes
    finally:
        engine.dispose()


def test_backfill_pinned_for_existing_active_trader(tmp_path: Path) -> None:
    """Un trader pré-M5 avec active=1 doit devenir pinned + status='pinned'.

    Ce test cible explicitement la révision 0003_m5_discovery (pas head),
    parce que les migrations ultérieures (0007 M5_bis) convertissent les
    paused en shadow, ce qui invaliderait l'assertion sur 0xinactive.
    Le comportement M5 de la migration 0003 reste inchangé.
    """
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "backfill.db"
    cfg = _make_config(db_path, project_root)

    # 1) Appliquer 0001+0002 pour obtenir le schéma pré-M5.
    command.upgrade(cfg, "0002_m4_pnl_snapshot")

    # 2) Insérer 2 traders : 1 actif (sera pinned après backfill), 1 inactif (sera paused).
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO target_traders (wallet_address, active, added_at) "
                    "VALUES (:w, :a, :t)"
                ),
                {"w": "0xactive", "a": 1, "t": "2026-01-01"},
            )
            conn.execute(
                text(
                    "INSERT INTO target_traders (wallet_address, active, added_at) "
                    "VALUES (:w, :a, :t)"
                ),
                {"w": "0xinactive", "a": 0, "t": "2026-01-01"},
            )
    finally:
        engine.dispose()

    # 3) Appliquer 0003 (pas head) → backfill M5 doit courir, point.
    command.upgrade(cfg, "0003_m5_discovery")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT wallet_address, active, status, pinned "
                    "FROM target_traders ORDER BY wallet_address"
                ),
            ).all()
        by_wallet = {r.wallet_address: r for r in rows}
        assert by_wallet["0xactive"].status == "pinned"
        assert by_wallet["0xactive"].pinned == 1
        assert by_wallet["0xinactive"].status == "paused"
        assert by_wallet["0xinactive"].pinned == 0
    finally:
        engine.dispose()


def test_downgrade_restores_pre_m5_schema(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "down.db"
    cfg = _make_config(db_path, project_root)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0002_m4_pnl_snapshot")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert "trader_scores" not in tables
        assert "trader_events" not in tables
        tt_cols = {c["name"] for c in inspector.get_columns("target_traders")}
        assert "status" not in tt_cols
        assert "pinned" not in tt_cols
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "bad_head",
    ["0004_nonexistent"],
)
def test_upgrade_unknown_revision_fails(tmp_path: Path, bad_head: str) -> None:
    """Sanity check : Alembic raise sur revision inconnue."""
    project_root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "bad.db"
    cfg = _make_config(db_path, project_root)
    with pytest.raises(Exception):  # noqa: B017 — alembic raises CommandError
        command.upgrade(cfg, bad_head)
