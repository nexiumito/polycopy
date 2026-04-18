#!/usr/bin/env python3
"""Backtest M12 : compare le scoring v1 et v2 sur un set labelé smart_money/random.

Produit un rapport JSON + HTML avec :

- Score v1 et v2 pour chaque wallet labelé.
- Rank v1 vs rank v2 + delta_rank.
- Brier aggregate sur le top-10 promu par chaque version (v1 vs v2).
- Spearman rank(v1, v2).
- Verdict : ``brier_v2 < brier_v1 - 0.01`` → **go cutover**. Sinon stay v1.

**Mode offline strict (v1 M12)** : le script consomme des fixtures JSON
pré-capturées dans ``tests/fixtures/scoring_v2/`` (1 fichier par wallet
labelé : ``positions_<wallet>.json``, ``activity_<wallet>.json``,
``daily_pnl_<wallet>.json``). La capture live est **hors scope** — l'utilisateur
doit préparer les fixtures avant le cutover.

Si une fixture manque pour un wallet, le wallet est **skipped** (log WARNING).
Le backtest reste déterministe même avec des fixtures partielles — le rapport
signale les wallets manquants pour guidance.

Usage ::

    python scripts/backtest_scoring_v2.py \\
        --labels-file assets/scoring_v2_labels.csv \\
        --fixtures-dir tests/fixtures/scoring_v2 \\
        --output /tmp/backtest_v2_report.html

Exit code ``0`` = succès (rapport généré).
Exit code ``2`` = Brier v2 ≥ Brier v1 - 0.01 (cutover **non** recommandé).
Exit code ``1`` = erreur fatale (fichier introuvable, schema invalide, etc.).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import html as html_module
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from polycopy.discovery.dtos import RawPosition, TraderMetrics
from polycopy.discovery.metrics_collector_v2 import (
    _compute_brier,
    _compute_calmar_from_curve,
    _compute_cash_pnl_90d,
    _compute_days_active,
    _compute_hhi_categories,
    _compute_monthly_pnl_positive_ratio,
    _compute_sizing_cv,
    _compute_sortino_from_curve,
    _compute_zombie_ratio,
)
from polycopy.discovery.scoring.v1 import _compute_score_v1
from polycopy.discovery.scoring.v2.aggregator import compute_score_v2
from polycopy.discovery.scoring.v2.dtos import PoolContext, TraderMetricsV2
from polycopy.storage.models import TraderDailyPnl

# Seuils fixe du verdict cutover (spec M12 §5.3).
_BRIER_SIGNIFICANCE_MARGIN: float = 0.01
_TOP_K: int = 10


@dataclass(frozen=True)
class LabeledWallet:
    wallet_address: str
    label: str  # "smart_money" | "random"
    notes: str


@dataclass(frozen=True)
class WalletBacktestResult:
    wallet: str
    label: str
    score_v1: float | None
    score_v2: float | None
    rank_v1: int | None
    rank_v2: int | None
    skipped_reason: str | None = None


def _read_labels(path: Path) -> list[LabeledWallet]:
    """Charge le CSV labels, ignore les lignes commentaire ``#``."""
    if not path.exists():
        raise FileNotFoundError(f"Labels file not found: {path}")
    labels: list[LabeledWallet] = []
    with path.open() as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].startswith("#") or row[0] == "wallet_address":
                continue
            if len(row) < 2:
                continue
            labels.append(
                LabeledWallet(
                    wallet_address=row[0].strip().lower(),
                    label=row[1].strip(),
                    notes=row[2].strip() if len(row) > 2 else "",
                ),
            )
    return labels


def _load_fixture(
    fixtures_dir: Path,
    wallet: str,
    prefix: str,
) -> list[dict[str, object]] | None:
    """Charge ``<prefix>_<wallet>.json`` ou retourne None si absent."""
    path = fixtures_dir / f"{prefix}_{wallet}.json"
    if not path.exists():
        return None
    try:
        return list(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError):
        return None


def _daily_pnl_from_fixture(
    fixtures_dir: Path,
    wallet: str,
) -> list[TraderDailyPnl]:
    """Charge `daily_pnl_<wallet>.json` et construit des `TraderDailyPnl` stubs.

    Format attendu : array d'objets `{date: "YYYY-MM-DD", equity_usdc,
    realized_pnl_day, unrealized_pnl_day, positions_count}`.
    """
    raw = _load_fixture(fixtures_dir, wallet, "daily_pnl")
    if not raw:
        return []
    rows: list[TraderDailyPnl] = []
    for item in raw:
        try:
            d = datetime.strptime(item["date"], "%Y-%m-%d").replace(tzinfo=UTC).date()
        except (KeyError, ValueError, TypeError):
            continue
        row = TraderDailyPnl(
            wallet_address=wallet,
            date=d,
            equity_usdc=float(item.get("equity_usdc", 0.0)),
            realized_pnl_day=float(item.get("realized_pnl_day", 0.0)),
            unrealized_pnl_day=float(item.get("unrealized_pnl_day", 0.0)),
            positions_count=int(item.get("positions_count", 0)),
            snapshotted_at=datetime.now(tz=UTC),
        )
        rows.append(row)
    return rows


def _build_metrics_from_fixtures(
    wallet: str,
    positions_raw: list[dict[str, object]],
    activity: list[dict[str, object]],
    curve_rows: list[TraderDailyPnl],
    cid_to_cat: dict[str, str],
) -> tuple[TraderMetrics, TraderMetricsV2]:
    """Reconstruit TraderMetrics + TraderMetricsV2 depuis les fixtures."""
    positions = [RawPosition.model_validate(p) for p in positions_raw]
    # TraderMetrics v1 reconstruit a minima (pour _compute_score_v1).
    resolved_count = sum(1 for p in positions if p.is_resolved)
    open_count = len(positions) - resolved_count
    wins = sum(1 for p in positions if p.is_resolved and float(p.cash_pnl) > 0)
    win_rate = wins / resolved_count if resolved_count else 0.0
    total_initial = sum(float(p.initial_value) for p in positions if p.is_resolved)
    total_realized = sum(float(p.realized_pnl) for p in positions if p.is_resolved)
    realized_roi = total_realized / total_initial if total_initial else 0.0
    # Volume from activity.
    total_volume = 0.0
    volume_per_market: dict[str, float] = {}
    for t in activity:
        cid = t.get("conditionId")
        if not isinstance(cid, str):
            continue
        size = float(t.get("size") or 0)
        price = float(t.get("price") or 0)
        notional = size * price
        total_volume += notional
        volume_per_market[cid] = volume_per_market.get(cid, 0.0) + notional
    if total_volume > 0 and volume_per_market:
        hhi = sum((v / total_volume) ** 2 for v in volume_per_market.values())
    else:
        hhi = 1.0
    base = TraderMetrics(
        wallet_address=wallet,
        resolved_positions_count=resolved_count,
        open_positions_count=open_count,
        win_rate=win_rate,
        realized_roi=realized_roi,
        total_volume_usd=total_volume,
        herfindahl_index=hhi,
        nb_distinct_markets=len(volume_per_market),
        largest_position_value_usd=max(
            (float(p.current_value) for p in positions),
            default=0.0,
        ),
        measurement_window_days=90,
        fetched_at=datetime.now(tz=UTC),
    )
    # Metrics v2 étendu.
    equity_curve = [float(r.equity_usdc) for r in curve_rows]
    metrics_v2 = TraderMetricsV2(
        base=base,
        sortino_90d=_compute_sortino_from_curve(equity_curve),
        calmar_90d=_compute_calmar_from_curve(equity_curve),
        brier_90d=_compute_brier(positions),
        timing_alpha_weighted=0.5,  # neutre v1 (§3.4)
        hhi_categories=_compute_hhi_categories(activity, cid_to_cat),
        monthly_pnl_positive_ratio=_compute_monthly_pnl_positive_ratio(curve_rows),
        zombie_ratio=_compute_zombie_ratio(positions),
        sizing_cv=_compute_sizing_cv(activity),
        cash_pnl_90d=_compute_cash_pnl_90d(positions),
        trade_count_90d=len(activity),
        days_active=_compute_days_active(activity),
        monthly_equity_curve=equity_curve,
    )
    return base, metrics_v2


def _build_pool_context(all_metrics: list[TraderMetricsV2]) -> PoolContext:
    """Agrège pool-wide values + brier baseline (même logique que orchestrator)."""
    from polycopy.discovery.scoring.v2.factors import (
        compute_calibration,
        compute_consistency,
        compute_discipline,
        compute_risk_adjusted,
        compute_specialization,
        compute_timing_alpha,
    )

    risk: list[float] = []
    calib: list[float] = []
    timing: list[float] = []
    spec: list[float] = []
    cons: list[float] = []
    disc: list[float] = []
    brier_values: list[float] = []
    for m in all_metrics:
        if m.brier_90d is not None:
            brier_values.append(float(m.brier_90d))
        risk.append(compute_risk_adjusted(m))
        calib.append(compute_calibration(m, brier_baseline_pool=0.25))
        timing.append(compute_timing_alpha(m))
        spec.append(compute_specialization(m))
        cons.append(compute_consistency(m))
        disc.append(compute_discipline(m))
    baseline = mean(brier_values) if brier_values else 0.25
    return PoolContext(
        risk_adjusted_pool=risk,
        calibration_pool=calib,
        timing_alpha_pool=timing,
        specialization_pool=spec,
        consistency_pool=cons,
        discipline_pool=disc,
        brier_baseline_pool=baseline,
    )


def _brier_aggregate_top_k(
    results: list[WalletBacktestResult],
    version: str,
    k: int = _TOP_K,
) -> float | None:
    """Brier aggregate du top-K promu par ``version``.

    **Approximation v1** : on considère "outcome=1" pour les wallets labelés
    ``smart_money``, "outcome=0" pour ``random``. ``predicted_prob = score`` (la
    formule elle-même est notre "prédiction"). Plus le score des smart_money
    est haut et celui des random bas, plus le Brier est bas → meilleure
    discrimination.
    """
    scored = [r for r in results if r.skipped_reason is None]
    attr = "score_v1" if version == "v1" else "score_v2"
    scored_with_version = [r for r in scored if getattr(r, attr) is not None]
    if not scored_with_version:
        return None
    # Top-K par score.
    top = sorted(
        scored_with_version,
        key=lambda r: getattr(r, attr) or 0.0,
        reverse=True,
    )[:k]
    if not top:
        return None
    sq_errors: list[float] = []
    for r in top:
        outcome = 1.0 if r.label == "smart_money" else 0.0
        pred = float(getattr(r, attr) or 0.0)
        sq_errors.append((outcome - pred) ** 2)
    return mean(sq_errors)


def _spearman(ranks_a: list[float], ranks_b: list[float]) -> float | None:
    n = min(len(ranks_a), len(ranks_b))
    if n < 3:
        return None
    d2 = sum((ranks_a[i] - ranks_b[i]) ** 2 for i in range(n))
    denom = n * (n * n - 1)
    if denom == 0:
        return None
    return 1.0 - (6.0 * d2) / denom


async def _run_backtest(
    labels: list[LabeledWallet],
    fixtures_dir: Path,
) -> dict[str, object]:
    """Pipeline principal. Retourne le rapport sous forme de dict sérialisable."""
    # 1. Build metrics v1 + v2 pour chaque wallet labelé (skip si fixtures manquantes).
    results_build: list[tuple[LabeledWallet, TraderMetricsV2 | None, str | None]] = []
    for lw in labels:
        positions_raw = _load_fixture(fixtures_dir, lw.wallet_address, "positions")
        activity = _load_fixture(fixtures_dir, lw.wallet_address, "activity")
        if positions_raw is None or activity is None:
            results_build.append((lw, None, "fixtures_missing"))
            continue
        curve_rows = _daily_pnl_from_fixture(fixtures_dir, lw.wallet_address)
        cid_to_cat: dict[str, str] = {}  # v1 backtest : pas de resolver (simplifié)
        try:
            _, metrics_v2 = _build_metrics_from_fixtures(
                wallet=lw.wallet_address,
                positions_raw=positions_raw,
                activity=activity,
                curve_rows=curve_rows,
                cid_to_cat=cid_to_cat,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            results_build.append((lw, None, f"build_failed:{exc}"))
            continue
        results_build.append((lw, metrics_v2, None))

    # 2. Pool context pool-wide.
    all_metrics = [m for _, m, _ in results_build if m is not None]
    pool_ctx = _build_pool_context(all_metrics) if all_metrics else None

    # 3. Score v1 + v2 pour chaque wallet.
    intermediate: list[WalletBacktestResult] = []
    for lw, metrics_v2, skip in results_build:
        if metrics_v2 is None:
            intermediate.append(
                WalletBacktestResult(
                    wallet=lw.wallet_address,
                    label=lw.label,
                    score_v1=None,
                    score_v2=None,
                    rank_v1=None,
                    rank_v2=None,
                    skipped_reason=skip,
                ),
            )
            continue
        score_v1 = _compute_score_v1(metrics_v2.base)
        score_v2 = compute_score_v2(metrics_v2, pool_ctx).score if pool_ctx else None
        intermediate.append(
            WalletBacktestResult(
                wallet=lw.wallet_address,
                label=lw.label,
                score_v1=score_v1,
                score_v2=score_v2,
                rank_v1=None,
                rank_v2=None,
            ),
        )

    # 4. Compute ranks.
    v1_sorted = sorted(
        [r for r in intermediate if r.score_v1 is not None],
        key=lambda r: r.score_v1 or 0.0,
        reverse=True,
    )
    v2_sorted = sorted(
        [r for r in intermediate if r.score_v2 is not None],
        key=lambda r: r.score_v2 or 0.0,
        reverse=True,
    )
    rank_v1 = {r.wallet: i + 1 for i, r in enumerate(v1_sorted)}
    rank_v2 = {r.wallet: i + 1 for i, r in enumerate(v2_sorted)}
    # Rebuild results with ranks.
    final_results = [
        WalletBacktestResult(
            wallet=r.wallet,
            label=r.label,
            score_v1=r.score_v1,
            score_v2=r.score_v2,
            rank_v1=rank_v1.get(r.wallet),
            rank_v2=rank_v2.get(r.wallet),
            skipped_reason=r.skipped_reason,
        )
        for r in intermediate
    ]

    # 5. Aggregates.
    brier_v1 = _brier_aggregate_top_k(final_results, "v1")
    brier_v2 = _brier_aggregate_top_k(final_results, "v2")
    both = [r for r in final_results if r.rank_v1 is not None and r.rank_v2 is not None]
    spearman = _spearman(
        [float(r.rank_v1 or 0) for r in both],
        [float(r.rank_v2 or 0) for r in both],
    )

    go_cutover = (
        brier_v1 is not None
        and brier_v2 is not None
        and brier_v2 < brier_v1 - _BRIER_SIGNIFICANCE_MARGIN
    )

    return {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "labels_count": len(labels),
        "scored_count": len([r for r in final_results if r.skipped_reason is None]),
        "skipped_count": len([r for r in final_results if r.skipped_reason is not None]),
        "brier_top10_v1": brier_v1,
        "brier_top10_v2": brier_v2,
        "brier_delta": (brier_v1 - brier_v2)
        if (brier_v1 is not None and brier_v2 is not None)
        else None,
        "significance_margin": _BRIER_SIGNIFICANCE_MARGIN,
        "spearman_rank": spearman,
        "go_cutover_recommended": go_cutover,
        "results": [
            {
                "wallet": r.wallet,
                "label": r.label,
                "score_v1": r.score_v1,
                "score_v2": r.score_v2,
                "rank_v1": r.rank_v1,
                "rank_v2": r.rank_v2,
                "skipped_reason": r.skipped_reason,
            }
            for r in final_results
        ],
    }


def _render_html_report(report: dict[str, object]) -> str:
    """Render HTML minimaliste du rapport (aucune dep, cohérent pnl_report.py)."""
    rows_html: list[str] = []
    results_list: list[dict[str, object]] = list(report["results"])  # type: ignore[arg-type]
    for r in results_list:
        wallet = str(r["wallet"])
        label = str(r["label"])
        score_v1 = r["score_v1"]
        score_v2 = r["score_v2"]
        rank_v1 = r["rank_v1"]
        rank_v2 = r["rank_v2"]
        skipped = str(r.get("skipped_reason") or "")
        v1_cell = f"{score_v1:.3f}" if isinstance(score_v1, (int, float)) else "—"
        v2_cell = f"{score_v2:.3f}" if isinstance(score_v2, (int, float)) else "—"
        rv1_cell = str(rank_v1) if rank_v1 is not None else "—"
        rv2_cell = str(rank_v2) if rank_v2 is not None else "—"
        rows_html.append(
            "<tr>"
            f"<td><code>{html_module.escape(wallet[:10])}…"
            f"{html_module.escape(wallet[-4:])}</code></td>"
            f"<td>{html_module.escape(label)}</td>"
            f"<td>{v1_cell}</td>"
            f"<td>{v2_cell}</td>"
            f"<td>{rv1_cell}</td>"
            f"<td>{rv2_cell}</td>"
            f"<td>{html_module.escape(skipped)}</td>"
            "</tr>",
        )
    verdict = (
        "<strong style='color: green;'>✓ CUTOVER RECOMMENDED</strong>"
        if report["go_cutover_recommended"]
        else "<strong style='color: orange;'>⚠ STAY ON V1 — no significant improvement</strong>"
    )
    brier_v1 = report["brier_top10_v1"]
    brier_v2 = report["brier_top10_v2"]
    brier_delta = report["brier_delta"]
    spearman = report["spearman_rank"]
    brier_v1_fmt = f"{brier_v1:.4f}" if isinstance(brier_v1, (int, float)) else "—"
    brier_v2_fmt = f"{brier_v2:.4f}" if isinstance(brier_v2, (int, float)) else "—"
    brier_delta_fmt = f"{brier_delta:.4f}" if isinstance(brier_delta, (int, float)) else "—"
    spearman_fmt = f"{spearman:.3f}" if isinstance(spearman, (int, float)) else "— (n < 3)"
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>polycopy — backtest scoring v2</title>
  <style>
    body {{
      font-family: Inter, system-ui, sans-serif; padding: 2rem;
      max-width: 900px; margin: auto;
    }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ padding: 6px 10px; text-align: left; border-bottom: 1px solid #ddd; }}
    th {{ background: #f6f6f6; }}
    .header-card {{ padding: 1rem; margin-bottom: 1rem; background: #f9fafb; border-radius: 6px; }}
  </style>
</head>
<body>
  <h1>Backtest scoring v2 — rapport M12</h1>
  <p><small>Généré le {html_module.escape(report["generated_at"])}</small></p>

  <div class="header-card">
    <h2>Verdict</h2>
    <p>{verdict}</p>
    <ul>
      <li>Wallets labelés : {report["labels_count"]}</li>
      <li>Wallets scorés : {report["scored_count"]}</li>
      <li>Wallets skipped (fixtures manquantes) : {report["skipped_count"]}</li>
      <li>Brier top-10 v1 : <strong>{brier_v1_fmt}</strong></li>
      <li>Brier top-10 v2 : <strong>{brier_v2_fmt}</strong></li>
      <li>Δ Brier (v1 - v2) : {brier_delta_fmt}
          (seuil de signification : {report["significance_margin"]})</li>
      <li>Spearman rank(v1, v2) : {spearman_fmt}</li>
    </ul>
  </div>

  <h2>Résultats par wallet</h2>
  <table>
    <thead>
      <tr>
        <th>Wallet</th><th>Label</th><th>v1</th><th>v2</th>
        <th>Rank v1</th><th>Rank v2</th><th>Skipped</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows_html)}
    </tbody>
  </table>
</body>
</html>
"""


async def _async_main(args: argparse.Namespace) -> int:
    # Script CLI standalone — les appels pathlib bloquants sont acceptables
    # (pas de serveur event-loop critique ici, cohérent avec scripts/pnl_report.py).
    labels_path = Path(args.labels_file).resolve()  # noqa: ASYNC240
    fixtures_dir = Path(args.fixtures_dir).resolve()  # noqa: ASYNC240
    output = Path(args.output).resolve()  # noqa: ASYNC240

    labels = _read_labels(labels_path)
    if not labels:
        print(f"[FATAL] no labels in {labels_path}", file=sys.stderr)
        return 1

    report = await _run_backtest(labels, fixtures_dir)

    # Write JSON next to HTML.
    json_path = output.with_suffix(".json")
    json_path.write_text(json.dumps(report, indent=2, default=str))  # noqa: ASYNC240

    # Write HTML.
    html = _render_html_report(report)
    output.write_text(html)  # noqa: ASYNC240

    # Logs stdout.
    print(f"✓ report JSON written to {json_path}")
    print(f"✓ report HTML written to {output}")
    print(f"  brier_top10_v1={report['brier_top10_v1']}")
    print(f"  brier_top10_v2={report['brier_top10_v2']}")
    print(f"  spearman={report['spearman_rank']}")
    print(f"  go_cutover={report['go_cutover_recommended']}")

    return 0 if report["go_cutover_recommended"] else 2


def main() -> None:
    parser = argparse.ArgumentParser(description="polycopy M12 scoring v2 backtest")
    parser.add_argument(
        "--labels-file",
        default="assets/scoring_v2_labels.csv",
        help="CSV wallets labelés smart_money/random.",
    )
    parser.add_argument(
        "--fixtures-dir",
        default="tests/fixtures/scoring_v2",
        help="Répertoire des fixtures JSON (positions_<w>.json, etc.).",
    )
    parser.add_argument(
        "--output",
        default="backtest_v2_report.html",
        help="Chemin HTML de sortie. Le JSON sera écrit à côté (.json).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
