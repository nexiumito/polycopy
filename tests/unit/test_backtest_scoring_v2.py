"""Tests `scripts/backtest_scoring_v2.py` (M12 §5.3, §8 étape 17).

Valide le plumbing (CSV labels parsing, rapport JSON/HTML produit, exit code),
pas la qualité statistique sur un vrai set labelé (ça dépend des fixtures
réelles que l'utilisateur prépare avant cutover).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _PROJECT_ROOT / "scripts" / "backtest_scoring_v2.py"
_LABELS_DEFAULT = _PROJECT_ROOT / "assets" / "scoring_v2_labels.csv"


def test_script_exists_and_is_executable() -> None:
    assert _SCRIPT.is_file()
    content = _SCRIPT.read_text()
    assert "#!/usr/bin/env python" in content.splitlines()[0]


def test_labels_file_committed() -> None:
    assert _LABELS_DEFAULT.is_file()
    lines = _LABELS_DEFAULT.read_text().splitlines()
    # Au moins 10 lignes non commentaires / header avec format csv minimal.
    data_rows = [
        line
        for line in lines
        if line.strip() and not line.startswith("#") and line != "wallet_address,label,notes"
    ]
    assert len(data_rows) >= 10, f"expected ≥ 10 labeled rows, got {len(data_rows)}"
    for row in data_rows[:10]:
        parts = row.split(",")
        assert len(parts) >= 2
        assert parts[0].startswith("0x")
        assert parts[1].strip() in {"smart_money", "random"}


def test_script_produces_report_empty_fixtures(tmp_path: Path) -> None:
    """Run script avec fixtures vides → rapport JSON + HTML produit, exit 2."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    output = tmp_path / "report.html"
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--labels-file",
            str(_LABELS_DEFAULT),
            "--fixtures-dir",
            str(fixtures_dir),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # Exit 2 = go_cutover=False (attendu : pas de fixture = tous wallets skipped)
    assert proc.returncode == 2, proc.stderr
    assert output.is_file()
    json_output = output.with_suffix(".json")
    assert json_output.is_file()
    report = json.loads(json_output.read_text())
    assert "brier_top10_v1" in report
    assert "brier_top10_v2" in report
    assert "spearman_rank" in report
    assert "go_cutover_recommended" in report
    assert report["go_cutover_recommended"] is False
    assert report["scored_count"] == 0
    assert report["skipped_count"] >= 10


def test_script_produces_html_report(tmp_path: Path) -> None:
    """HTML contient les sections Verdict + Résultats par wallet."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    output = tmp_path / "report.html"
    subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--labels-file",
            str(_LABELS_DEFAULT),
            "--fixtures-dir",
            str(fixtures_dir),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    html = output.read_text()
    assert "<h1>Backtest scoring v2 — rapport M12</h1>" in html
    assert "Verdict" in html
    assert "CUTOVER" in html or "STAY ON V1" in html


def test_script_fails_on_missing_labels_file(tmp_path: Path) -> None:
    """``--labels-file`` introuvable → exit 1 + message stderr."""
    output = tmp_path / "report.html"
    proc = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--labels-file",
            str(tmp_path / "nonexistent.csv"),
            "--fixtures-dir",
            str(tmp_path),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1


def _write_minimal_fixtures(fixtures_dir: Path, wallet: str) -> None:
    """Écrit les 3 fixtures minimales pour 1 wallet (scoring non-trivial)."""
    from datetime import UTC, datetime, timedelta

    positions = [
        {
            "conditionId": "0xcid1",
            "asset": "0xtoken",
            "size": 0.0,
            "avgPrice": 0.3,
            "initialValue": 100.0,
            "currentValue": 0.0,
            "cashPnl": 50.0,
            "realizedPnl": 50.0,
            "totalBought": 100.0,
            "redeemable": True,
        }
        for _ in range(10)
    ]
    now = datetime.now(tz=UTC)
    activity = [
        {
            "conditionId": "0xcid1",
            "size": 10.0,
            "price": 0.3,
            "timestamp": int((now - timedelta(days=i)).timestamp()),
        }
        for i in range(60)
    ]
    daily_pnl = [
        {
            "date": (now - timedelta(days=29 - i)).date().isoformat(),
            "equity_usdc": 100.0 + i * 2.0,  # croissance régulière
            "realized_pnl_day": 2.0,
            "unrealized_pnl_day": 0.0,
            "positions_count": 5,
        }
        for i in range(30)
    ]
    (fixtures_dir / f"positions_{wallet}.json").write_text(json.dumps(positions))
    (fixtures_dir / f"activity_{wallet}.json").write_text(json.dumps(activity))
    (fixtures_dir / f"daily_pnl_{wallet}.json").write_text(json.dumps(daily_pnl))


def test_script_scores_wallets_when_fixtures_present(tmp_path: Path) -> None:
    """Fixtures minimales pour 2 wallets → scoring effectif + rapport."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    _write_minimal_fixtures(fixtures_dir, "0xa5ef39c3d3e10d0b270233af41cac69796b12966")
    _write_minimal_fixtures(fixtures_dir, "0x2e3ea056400d81c42e2ce26ef25fda4ec5caabea")

    output = tmp_path / "report.html"
    subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--labels-file",
            str(_LABELS_DEFAULT),
            "--fixtures-dir",
            str(fixtures_dir),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    json_output = output.with_suffix(".json")
    report = json.loads(json_output.read_text())
    # ≥ 2 wallets scorés (peut-être plus si autres fixtures existent)
    assert report["scored_count"] >= 2


def test_script_json_report_includes_per_wallet_rows(tmp_path: Path) -> None:
    """JSON rapport contient liste ``results`` avec 1 row par label."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    output = tmp_path / "report.html"
    subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--labels-file",
            str(_LABELS_DEFAULT),
            "--fixtures-dir",
            str(fixtures_dir),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    report = json.loads(output.with_suffix(".json").read_text())
    assert isinstance(report["results"], list)
    assert len(report["results"]) >= 10
    for row in report["results"]:
        assert "wallet" in row
        assert "label" in row
        assert "score_v1" in row
        assert "score_v2" in row


def test_spearman_rank_edge_cases_in_script() -> None:
    """`_spearman` retourne None pour n < 3, ±1.0 pour ranks parfaits.

    Chargement via `runpy` pour éviter le bug dataclasses lors du load
    isolé via `importlib.util` (cls.__module__ None quand le module n'est
    pas enregistré dans sys.modules en amont).
    """
    import importlib.util
    import sys

    module_name = "_backtest_v2_test_load"
    spec = importlib.util.spec_from_file_location(module_name, _SCRIPT)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod  # required for dataclass __module__ lookup
    try:
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        assert mod._spearman([1.0, 2.0], [1.0, 2.0]) is None
        assert mod._spearman([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)
        assert mod._spearman([1.0, 2.0, 3.0], [3.0, 2.0, 1.0]) == pytest.approx(-1.0)
    finally:
        sys.modules.pop(module_name, None)
