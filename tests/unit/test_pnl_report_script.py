"""Tests smoke du script ``scripts/pnl_report.py``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from polycopy.storage.models import Base, PnlSnapshot

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import pnl_report  # type: ignore[import-not-found]  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "pnl.db"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    return db_path


def _run(argv: list[str]) -> int:
    return pnl_report.main(argv)


def test_stdout_empty_db_ok(tmp_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = _run(["--db", f"sqlite:///{tmp_db}", "--output", "stdout", "--since", "1"])
    assert code == 0
    out = capsys.readouterr().out
    assert "PnL report" in out
    assert "no snapshots found" in out


def test_csv_with_snapshots(tmp_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from datetime import UTC, datetime

    engine = create_engine(f"sqlite:///{tmp_db}")
    with engine.begin() as conn:
        for i in range(3):
            conn.execute(
                PnlSnapshot.__table__.insert().values(
                    timestamp=datetime.now(tz=UTC),
                    total_usdc=100.0 + i,
                    realized_pnl=0.0,
                    unrealized_pnl=0.0,
                    drawdown_pct=0.0,
                    open_positions_count=0,
                    cash_pnl_total=None,
                    is_dry_run=False,
                ),
            )
    engine.dispose()
    code = _run(["--db", f"sqlite:///{tmp_db}", "--output", "csv", "--since", "1"])
    assert code == 0
    out = capsys.readouterr().out
    # 1 header + 3 data rows.
    assert out.count("\n") >= 4


def test_html_writes_file(tmp_db: Path, tmp_path: Path) -> None:
    out_file = tmp_path / "out.html"
    code = _run(
        [
            "--db",
            f"sqlite:///{tmp_db}",
            "--output",
            "html",
            "--since",
            "1",
            "--output-file",
            str(out_file),
        ],
    )
    assert code == 0
    assert out_file.exists()
    content = out_file.read_text()
    assert "<!doctype html>" in content
    assert "polycopy" in content
