"""Validation H-EMP-1 + H-EMP-2 avant ship MA.8 (M14 §14.4).

Lit un dump SQL ou DB SQLite `trader_scores` historique et calcule :

- **H-EMP-1** : décomposition de la variance totale par facteur normalisé
  (sur les rows ``scoring_version='v2'``). Hypothèse : ``risk_adjusted``
  contribue ≥ 40 % de la variance totale (cf. Claude §3.1).
- **H-EMP-2** : variance cycle-to-cycle relative par wallet
  (``pstdev / mean``). Hypothèse : σ relatif < 10 % sur ≥ 80 % des wallets
  ACTIVE (cf. spec MA.md §6).

Sortie : rapport texte (stdout) + optionnel HTML/JSON. Exit code 0 si les
2 hypothèses passent leur seuil go, 1 sinon.

Usage :
    python scripts/validate_ma_hypotheses.py \
        --db /home/nexium/code/polycopy/polycopy.db
    python scripts/validate_ma_hypotheses.py \
        --sql-dump tests/fixtures/h_emp_280_cycles.sql

Cf. spec M14 §14.4 + brief MA.md §6.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

# Seuils go (M14 §1.4 + MA.md §6).
H_EMP_1_RISK_ADJUSTED_MIN_PCT: float = 0.40
H_EMP_2_RELATIVE_SIGMA_MAX: float = 0.10
H_EMP_2_PCT_WALLETS_UNDER_THRESHOLD_MIN: float = 0.80

_FACTORS = (
    "risk_adjusted",
    "calibration",
    "timing_alpha",
    "specialization",
    "consistency",
    "discipline",
)


def _load_v2_rows_from_db(db_path: Path) -> list[dict[str, Any]]:
    """Charge les rows trader_scores v2 (dict avec wallet, score, cycle_at, breakdown)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT wallet_address, score, cycle_at, metrics_snapshot "
        "FROM trader_scores WHERE scoring_version = 'v2' ORDER BY cycle_at"
    )
    rows: list[dict[str, Any]] = []
    for r in cur.fetchall():
        try:
            snapshot = json.loads(r["metrics_snapshot"])
        except (TypeError, json.JSONDecodeError):
            continue
        normalized = snapshot.get("v2_normalized") or snapshot.get(
            "breakdown", {},
        ).get("normalized")
        if not normalized:
            continue
        rows.append({
            "wallet_address": r["wallet_address"],
            "score": float(r["score"]),
            "cycle_at": r["cycle_at"],
            "normalized": normalized,
        })
    conn.close()
    return rows


def _load_v2_rows_from_sql_dump(dump_path: Path) -> list[dict[str, Any]]:
    """Reconstruit une DB temp depuis un dump SQL puis appelle ``_load_v2_rows_from_db``."""
    sql = dump_path.read_text(encoding="utf-8")
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    conn = sqlite3.connect(str(tmp_path))
    conn.executescript(sql)
    conn.close()
    rows = _load_v2_rows_from_db(tmp_path)
    tmp_path.unlink(missing_ok=True)
    return rows


def compute_h_emp_1(rows: list[dict[str, Any]]) -> dict[str, float]:
    """H-EMP-1 : variance par facteur normalisé / variance totale.

    Retourne ``{factor_name: contribution_pct}`` (sommes ≈ 1.0).
    """
    by_factor: dict[str, float] = {}
    for factor in _FACTORS:
        values = [
            float(r["normalized"].get(factor, 0.0))
            for r in rows
            if factor in r["normalized"]
        ]
        if len(values) >= 2:
            by_factor[factor] = statistics.pvariance(values)
        else:
            by_factor[factor] = 0.0
    total_var = sum(by_factor.values())
    if total_var == 0:
        return dict.fromkeys(_FACTORS, 0.0)
    return {f: v / total_var for f, v in by_factor.items()}


def compute_h_emp_2(rows: list[dict[str, Any]]) -> dict[str, float]:
    """H-EMP-2 : variance cycle-to-cycle relative par wallet (pstdev / mean).

    Retourne ``{wallet_address: relative_sigma}``. Wallets avec < 5 cycles
    exclus (pas assez de samples).
    """
    by_wallet: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        by_wallet[r["wallet_address"]].append(float(r["score"]))
    relative_sigmas: dict[str, float] = {}
    for wallet, scores in by_wallet.items():
        if len(scores) < 5:
            continue
        avg = statistics.mean(scores)
        if avg <= 0.0:
            continue
        relative_sigmas[wallet] = statistics.pstdev(scores) / avg
    return relative_sigmas


def render_report(
    *,
    rows: list[dict[str, Any]],
    h_emp_1: dict[str, float],
    h_emp_2: dict[str, float],
    h_emp_1_pass: bool,
    h_emp_2_pass: bool,
) -> str:
    """Format un rapport texte pour stdout."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("M14 H-EMP validation report")
    lines.append("=" * 72)
    lines.append(f"Total v2 rows analyzed: {len(rows)}")
    lines.append(
        f"Distinct wallets with >= 5 cycles: {len(h_emp_2)}"
    )
    lines.append("")
    lines.append("--- H-EMP-1 : variance contribution by factor (target: risk_adjusted >= 40%) ---")
    for factor in _FACTORS:
        pct = h_emp_1.get(factor, 0.0)
        bar = "#" * int(pct * 40)
        lines.append(f"  {factor:18s} {pct:6.1%}  {bar}")
    risk_pct = h_emp_1.get("risk_adjusted", 0.0)
    status_1 = "[OK]" if h_emp_1_pass else "[FAIL]"
    lines.append(
        f"  -> risk_adjusted = {risk_pct:.1%} "
        f"(need >= {H_EMP_1_RISK_ADJUSTED_MIN_PCT:.0%}) {status_1}",
    )
    lines.append("")
    lines.append("--- H-EMP-2 : cycle-to-cycle relative sigma per wallet ---")
    if h_emp_2:
        sorted_sigmas = sorted(h_emp_2.values())
        median_sigma = statistics.median(sorted_sigmas)
        max_sigma = max(sorted_sigmas)
        pct_under = sum(
            1 for s in sorted_sigmas if s < H_EMP_2_RELATIVE_SIGMA_MAX
        ) / len(sorted_sigmas)
        lines.append(f"  Wallets analyzed     : {len(sorted_sigmas)}")
        lines.append(f"  Median relative sigma: {median_sigma:.1%}")
        lines.append(f"  Max relative sigma   : {max_sigma:.1%}")
        lines.append(
            f"  Pct wallets sigma < {H_EMP_2_RELATIVE_SIGMA_MAX:.0%}: "
            f"{pct_under:.1%} (need >= {H_EMP_2_PCT_WALLETS_UNDER_THRESHOLD_MIN:.0%})"
        )
        status_2 = "[OK]" if h_emp_2_pass else "[FAIL]"
        lines.append(f"  -> {status_2}")
    else:
        lines.append("  No wallets with >= 5 cycles. Skipped.")
    lines.append("")
    lines.append("=" * 72)
    overall = (
        "[H-EMP VALIDATION PASSED]"
        if h_emp_1_pass and h_emp_2_pass
        else "[H-EMP VALIDATION FAILED]"
    )
    lines.append(f"Overall: {overall}")
    lines.append("=" * 72)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--db", type=Path, help="Path to SQLite DB file (e.g. polycopy.db).")
    src.add_argument("--sql-dump", type=Path, help="Path to SQL dump file.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write the report (text). If omitted, only stdout.",
    )
    args = parser.parse_args()

    rows = (
        _load_v2_rows_from_db(args.db)
        if args.db
        else _load_v2_rows_from_sql_dump(args.sql_dump)
    )

    if not rows:
        print("ERROR: no v2 rows found in source. Did you run shadow period?", file=sys.stderr)
        return 1

    h_emp_1 = compute_h_emp_1(rows)
    h_emp_2 = compute_h_emp_2(rows)

    risk_pct = h_emp_1.get("risk_adjusted", 0.0)
    h_emp_1_pass = risk_pct >= H_EMP_1_RISK_ADJUSTED_MIN_PCT

    if h_emp_2:
        pct_under = sum(
            1 for s in h_emp_2.values() if s < H_EMP_2_RELATIVE_SIGMA_MAX
        ) / len(h_emp_2)
        h_emp_2_pass = pct_under >= H_EMP_2_PCT_WALLETS_UNDER_THRESHOLD_MIN
    else:
        h_emp_2_pass = False

    report = render_report(
        rows=rows,
        h_emp_1=h_emp_1,
        h_emp_2=h_emp_2,
        h_emp_1_pass=h_emp_1_pass,
        h_emp_2_pass=h_emp_2_pass,
    )
    print(report)
    if args.output:
        args.output.write_text(report, encoding="utf-8")

    return 0 if (h_emp_1_pass and h_emp_2_pass) else 1


if __name__ == "__main__":
    sys.exit(main())
