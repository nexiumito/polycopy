"""Tests du script `scripts/score_backtest.py` (mode `--no-network`)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.score_backtest import _spearman_rank_corr

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _PROJECT_ROOT / "scripts" / "score_backtest.py"


def test_spearman_perfect_positive() -> None:
    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [10.0, 20.0, 30.0, 40.0]
    assert _spearman_rank_corr(xs, ys) == pytest.approx(1.0)


def test_spearman_perfect_negative() -> None:
    xs = [1.0, 2.0, 3.0, 4.0]
    ys = [40.0, 30.0, 20.0, 10.0]
    assert _spearman_rank_corr(xs, ys) == pytest.approx(-1.0)


def test_spearman_zero_on_small_sample() -> None:
    assert _spearman_rank_corr([1.0], [1.0]) == 0.0


def test_spearman_handles_ties() -> None:
    xs = [1.0, 1.0, 2.0, 3.0]
    ys = [5.0, 5.0, 10.0, 15.0]
    # Ranks moyennes sur ties → correlation non-nan et proche de 1.
    corr = _spearman_rank_corr(xs, ys)
    assert corr == pytest.approx(1.0, abs=1e-9)


def test_backtest_script_runs_in_no_network_mode(tmp_path: Path) -> None:
    """Smoke CLI : mode `--no-network` produit un HTML sans toucher le réseau."""
    seed_file = tmp_path / "seed.txt"
    seed_file.write_text(
        "# test seed\n" + "\n".join(f"0x{i:040x}" for i in range(10)) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.html"
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--wallets-file",
            str(seed_file),
            "--as-of",
            "2026-01-15",
            "--observe-days",
            "30",
            "--output",
            str(output),
            "--no-network",
        ],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    # Exit 0 ou 2 (verdict Spearman) — jamais crash.
    assert result.returncode in (0, 2), result.stderr
    assert output.exists()
    content = output.read_text()
    assert "polycopy" in content
    assert "Corrélation Spearman" in content


def test_backtest_script_csv_output(tmp_path: Path) -> None:
    seed_file = tmp_path / "seed.txt"
    seed_file.write_text(
        "\n".join(f"0x{i:040x}" for i in range(8)) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.csv"
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--wallets-file",
            str(seed_file),
            "--as-of",
            "2026-01-15",
            "--format",
            "csv",
            "--output",
            str(output),
            "--no-network",
        ],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 2), result.stderr
    text = output.read_text()
    assert "spearman_corr=" in text
    assert "wallet,score_at_t" in text


def test_backtest_script_empty_seed_fails(tmp_path: Path) -> None:
    seed = tmp_path / "empty.txt"
    seed.write_text("# only comments\n", encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--wallets-file",
            str(seed),
            "--as-of",
            "2026-01-15",
            "--no-network",
        ],
        cwd=_PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "no wallets" in result.stderr.lower()
