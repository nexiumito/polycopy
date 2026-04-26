"""Tests de la migration Alembic 0007 (M5_bis competitive eviction).

Couvre : schéma (widen status + nouvelles colonnes), data migration
paused→shadow avec previously_demoted_at, idempotence, downgrade/upgrade
bi-directionnel. Cf. spec docs/specs/M5_bis_competitive_eviction_spec.md §6.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command


def _make_config(db_path: Path, project_root: Path) -> Config:
    cfg = Config(str(project_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_upgrade_head_adds_eviction_columns(tmp_path: Path) -> None:
    """target_traders gagne 3 colonnes nullables + status widen à 16 chars."""
    cfg = _make_config(tmp_path / "upgrade.db", _project_root())
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    try:
        inspector = inspect(engine)
        tt_cols = {c["name"]: c for c in inspector.get_columns("target_traders")}
        assert "previously_demoted_at" in tt_cols
        assert "eviction_state_entered_at" in tt_cols
        assert "eviction_triggering_wallet" in tt_cols
        # Les 3 sont nullable (additif strict).
        assert tt_cols["previously_demoted_at"]["nullable"]
        assert tt_cols["eviction_state_entered_at"]["nullable"]
        assert tt_cols["eviction_triggering_wallet"]["nullable"]
        # status widen à VARCHAR(16).
        status_type = str(tt_cols["status"]["type"]).upper()
        assert "VARCHAR(16)" in status_type or "CHAR(16)" in status_type, status_type
    finally:
        engine.dispose()


def test_upgrade_head_widens_trader_events_status_columns(tmp_path: Path) -> None:
    """trader_events.from_status et to_status passent de String(8) à String(16)."""
    cfg = _make_config(tmp_path / "widen.db", _project_root())
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{tmp_path / 'widen.db'}")
    try:
        inspector = inspect(engine)
        te_cols = {c["name"]: c for c in inspector.get_columns("trader_events")}
        from_type = str(te_cols["from_status"]["type"]).upper()
        to_type = str(te_cols["to_status"]["type"]).upper()
        assert "VARCHAR(16)" in from_type or "CHAR(16)" in from_type, from_type
        assert "VARCHAR(16)" in to_type or "CHAR(16)" in to_type, to_type
    finally:
        engine.dispose()


def test_data_migration_paused_to_shadow_with_flag(tmp_path: Path) -> None:
    """Un paused M5 devient shadow + previously_demoted_at posé à last_scored_at.

    Cas testés :
      - paused avec last_scored_at → previously_demoted_at = last_scored_at.
      - paused sans last_scored_at → previously_demoted_at = CURRENT_TIMESTAMP (non NULL).
      - active/shadow/pinned inchangés (pas de previously_demoted_at).
    """
    db_path = tmp_path / "backfill.db"
    cfg = _make_config(db_path, _project_root())

    # 1) Appliquer jusqu'à 0006 (pré-M5_bis).
    command.upgrade(cfg, "0006_m12_trader_daily_pnl")

    # 2) Seed 5 wallets couvrant tous les status M5.
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO target_traders "
                    "(wallet_address, active, added_at, status, pinned, "
                    "consecutive_low_score_cycles, last_scored_at) VALUES "
                    "('0xa', 0, '2026-01-01', 'paused', 0, 3, '2026-04-01 10:00:00'),"
                    "('0xb', 1, '2026-01-01', 'active', 0, 0, '2026-04-21 10:00:00'),"
                    "('0xc', 0, '2026-01-01', 'shadow', 0, 0, NULL),"
                    "('0xd', 1, '2026-01-01', 'pinned', 1, 0, '2026-04-21 09:00:00'),"
                    "('0xe', 0, '2026-01-01', 'paused', 0, 3, NULL)",
                ),
            )
    finally:
        engine.dispose()

    # 3) Upgrade vers 0007.
    command.upgrade(cfg, "head")

    # 4) Vérifier le backfill.
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT wallet_address, status, active, previously_demoted_at "
                    "FROM target_traders ORDER BY wallet_address",
                ),
            ).all()
        by_wallet = {r.wallet_address: r for r in rows}

        # 0xa : paused → shadow, previously_demoted_at = son last_scored_at.
        assert by_wallet["0xa"].status == "shadow"
        assert by_wallet["0xa"].active == 0
        assert by_wallet["0xa"].previously_demoted_at is not None
        assert "2026-04-01" in str(by_wallet["0xa"].previously_demoted_at)

        # 0xe : paused → shadow, previously_demoted_at = CURRENT_TIMESTAMP (non NULL).
        assert by_wallet["0xe"].status == "shadow"
        assert by_wallet["0xe"].active == 0
        assert by_wallet["0xe"].previously_demoted_at is not None

        # 0xb, 0xc, 0xd inchangés, previously_demoted_at NULL.
        assert by_wallet["0xb"].status == "active"
        assert by_wallet["0xb"].previously_demoted_at is None
        assert by_wallet["0xc"].status == "shadow"
        assert by_wallet["0xc"].previously_demoted_at is None
        assert by_wallet["0xd"].status == "pinned"
        assert by_wallet["0xd"].previously_demoted_at is None
    finally:
        engine.dispose()


def test_upgrade_is_idempotent_second_run_noop(tmp_path: Path) -> None:
    """Re-run `alembic upgrade head` ne modifie rien (Alembic tracking)."""
    db_path = tmp_path / "idempotent.db"
    cfg = _make_config(db_path, _project_root())
    command.upgrade(cfg, "head")
    # Re-run : doit être no-op (Alembic lit alembic_version et skip).
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            rev = conn.execute(
                text("SELECT version_num FROM alembic_version"),
            ).scalar_one()
        # M15 MB.1 : migration head est désormais 0009 (M5_bis ne casse pas
        # par cet upgrade — c'est un check de cohérence Alembic, pas un
        # ancrage M5_bis-only).
        assert rev == "0009_m15_anti_toxic_lifecycle"
    finally:
        engine.dispose()


def test_data_migration_idempotent_paused_absent_on_second_run(tmp_path: Path) -> None:
    """Après upgrade → downgrade → re-upgrade, la data migration reste saine.

    Scénario : on upgrade, puis on downgrade (paused revient), puis on
    re-upgrade → la data migration re-convertit paused en shadow sans
    doublonner previously_demoted_at.
    """
    db_path = tmp_path / "cycle.db"
    cfg = _make_config(db_path, _project_root())

    command.upgrade(cfg, "0006_m12_trader_daily_pnl")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO target_traders "
                    "(wallet_address, active, added_at, status, pinned, "
                    "consecutive_low_score_cycles, last_scored_at) VALUES "
                    "('0xcycle', 0, '2026-01-01', 'paused', 0, 3, "
                    "'2026-04-01 10:00:00')",
                ),
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0006_m12_trader_daily_pnl")
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT status, previously_demoted_at FROM target_traders "
                    "WHERE wallet_address = '0xcycle'",
                ),
            ).one()
        assert row.status == "shadow"
        assert row.previously_demoted_at is not None
    finally:
        engine.dispose()


def test_downgrade_reverses_schema(tmp_path: Path) -> None:
    """Downgrade 0007 → 0006 retire les 3 colonnes + remet status en String(8)."""
    db_path = tmp_path / "down.db"
    cfg = _make_config(db_path, _project_root())
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0006_m12_trader_daily_pnl")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        tt_cols = {c["name"]: c for c in inspector.get_columns("target_traders")}
        assert "previously_demoted_at" not in tt_cols
        assert "eviction_state_entered_at" not in tt_cols
        assert "eviction_triggering_wallet" not in tt_cols
        # Status rétréci à String(8).
        status_type = str(tt_cols["status"]["type"]).upper()
        assert "VARCHAR(8)" in status_type or "CHAR(8)" in status_type, status_type
    finally:
        engine.dispose()


def test_downgrade_reverses_paused_shadow_data_migration(tmp_path: Path) -> None:
    """Un shadow avec previously_demoted_at posé redevient paused en downgrade."""
    db_path = tmp_path / "down_data.db"
    cfg = _make_config(db_path, _project_root())

    command.upgrade(cfg, "0006_m12_trader_daily_pnl")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO target_traders "
                    "(wallet_address, active, added_at, status, pinned, "
                    "consecutive_low_score_cycles, last_scored_at) VALUES "
                    "('0xrev', 0, '2026-01-01', 'paused', 0, 3, "
                    "'2026-04-01 10:00:00')",
                ),
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0006_m12_trader_daily_pnl")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            status = conn.execute(
                text(
                    "SELECT status FROM target_traders WHERE wallet_address='0xrev'",
                ),
            ).scalar_one()
        assert status == "paused"
    finally:
        engine.dispose()
