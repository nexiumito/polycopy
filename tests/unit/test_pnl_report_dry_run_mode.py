"""Tests M8 §9.10 — ``scripts/pnl_report.py --dry-run-mode``."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from polycopy.storage.models import Base, MyOrder, MyPosition, PnlSnapshot

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import pnl_report  # type: ignore[import-not-found]  # noqa: E402


def _seed_mixed_snapshots(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for i, is_dry in [(0, False), (1, False), (2, True), (3, True)]:
            conn.execute(
                PnlSnapshot.__table__.insert().values(
                    timestamp=datetime.now(tz=UTC),
                    total_usdc=1000.0 + i * 5,
                    realized_pnl=0.0,
                    unrealized_pnl=0.0,
                    drawdown_pct=0.0,
                    open_positions_count=0,
                    cash_pnl_total=None,
                    is_dry_run=is_dry,
                ),
            )
        conn.execute(
            MyOrder.__table__.insert().values(
                source_tx_hash="0x1",
                condition_id="0xc",
                asset_id="A",
                side="BUY",
                size=10.0,
                price=0.5,
                tick_size=0.01,
                neg_risk=False,
                order_type="FOK",
                status="SIMULATED",
                transaction_hashes=[],
                simulated=True,
                realistic_fill=True,
                sent_at=datetime.now(tz=UTC),
            ),
        )
        conn.execute(
            MyPosition.__table__.insert().values(
                condition_id="0xc",
                asset_id="A",
                size=10.0,
                avg_price=0.5,
                opened_at=datetime.now(tz=UTC),
                simulated=True,
            ),
        )
    engine.dispose()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "pnl.db"


def test_dry_run_mode_filters_to_dry_only(db_path: Path, tmp_path: Path) -> None:
    _seed_mixed_snapshots(db_path)
    out_file = tmp_path / "dry.html"
    code = pnl_report.main(
        [
            "--db",
            f"sqlite:///{db_path}",
            "--output",
            "html",
            "--since",
            "1",
            "--output-file",
            str(out_file),
            "--dry-run-mode",
        ],
    )
    assert code == 0
    body = out_file.read_text()
    # Les valeurs des snapshots dry sont 1010 / 1015, les real sont 1000 / 1005.
    assert "1010.00" in body
    assert "1015.00" in body
    # Les real ne doivent PAS apparaître dans le tableau.
    assert "1000.00" not in body
    # Position virtuelle apparait
    assert "Open positions" in body


def test_dry_run_mode_default_output_filename_when_html(
    db_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_mixed_snapshots(db_path)
    monkeypatch.chdir(tmp_path)
    code = pnl_report.main(
        [
            "--db",
            f"sqlite:///{db_path}",
            "--output",
            "html",
            "--since",
            "1",
            "--dry-run-mode",
        ],
    )
    assert code == 0
    assert (tmp_path / "dry_run_pnl_report.html").exists()


def test_dry_run_mode_empty_db_writes_no_data_message(
    db_path: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    code = pnl_report.main(
        [
            "--db",
            f"sqlite:///{db_path}",
            "--output",
            "stdout",
            "--since",
            "1",
            "--dry-run-mode",
        ],
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "no snapshots" in out
