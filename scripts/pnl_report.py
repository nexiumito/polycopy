#!/usr/bin/env python3
"""Rapport PnL : lit ``pnl_snapshots`` + ``my_orders`` + ``my_positions``.

Script sync (pas d'``asyncio.run``) — c'est un outil ponctuel, pas une boucle
event-loop. Utilise ``sqlalchemy`` sync via ``create_engine`` sur la même DB.

Usage :

    python scripts/pnl_report.py --since 7 --output html

Output formats :
- ``stdout`` : table plain text (default).
- ``csv``    : 1 ligne par snapshot sur stdout.
- ``html``   : génère ``pnl_report.html`` avec sparkline SVG inline.

Voir ``specs/M4-monitoring.md`` §6.
"""

from __future__ import annotations

import argparse
import csv
import html as html_module
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from polycopy.config import settings
from polycopy.storage.models import MyOrder, MyPosition, PnlSnapshot


def _sync_db_url(url: str) -> str:
    for async_prefix, sync_prefix in {
        "sqlite+aiosqlite://": "sqlite://",
        "postgresql+asyncpg://": "postgresql://",
    }.items():
        if url.startswith(async_prefix):
            return sync_prefix + url[len(async_prefix) :]
    return url


@dataclass(frozen=True)
class ReportStats:
    """Agrégations calculées par ``_collect_stats``."""

    snapshots: list[PnlSnapshot]
    latest: PnlSnapshot | None
    max_drawdown_pct: float
    total_usdc_delta: float
    orders_by_status: dict[str, int]
    open_positions_count: int
    open_positions_notional: float
    since_utc: datetime


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pnl_report",
        description="Rapport PnL polycopy (lit la DB locale).",
    )
    parser.add_argument("--since", type=int, default=7, help="Nb jours d'historique.")
    parser.add_argument(
        "--output",
        choices=["stdout", "csv", "html"],
        default="html",
        help="Format de sortie.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="URL DB override (default : settings.database_url).",
    )
    parser.add_argument(
        "--output-file",
        default="pnl_report.html",
        help="Fichier HTML généré (uniquement pour --output html).",
    )
    parser.add_argument(
        "--include-dry-run",
        action="store_true",
        help="Inclure les snapshots marqués is_dry_run=True.",
    )
    return parser.parse_args(argv)


def _collect_stats(
    session: Session,
    since_days: int,
    *,
    include_dry_run: bool,
) -> ReportStats:
    since = datetime.now(tz=UTC) - timedelta(days=since_days)

    snapshots_stmt = select(PnlSnapshot).where(PnlSnapshot.timestamp >= since)
    if not include_dry_run:
        snapshots_stmt = snapshots_stmt.where(PnlSnapshot.is_dry_run.is_(False))
    snapshots = list(
        session.execute(snapshots_stmt.order_by(PnlSnapshot.timestamp.asc())).scalars().all(),
    )

    latest = snapshots[-1] if snapshots else None
    max_drawdown = max((s.drawdown_pct for s in snapshots), default=0.0)
    total_delta = snapshots[-1].total_usdc - snapshots[0].total_usdc if len(snapshots) >= 2 else 0.0

    status_rows = session.execute(
        select(MyOrder.status, func.count(MyOrder.id)).group_by(MyOrder.status),
    ).all()
    orders_by_status = {row[0]: int(row[1]) for row in status_rows}

    open_positions = list(
        session.execute(select(MyPosition).where(MyPosition.closed_at.is_(None))).scalars().all(),
    )
    open_notional = sum(p.size * p.avg_price for p in open_positions)

    return ReportStats(
        snapshots=snapshots,
        latest=latest,
        max_drawdown_pct=max_drawdown,
        total_usdc_delta=total_delta,
        orders_by_status=orders_by_status,
        open_positions_count=len(open_positions),
        open_positions_notional=open_notional,
        since_utc=since,
    )


def _render_sparkline_svg(
    timestamps: list[datetime],
    values: list[float],
    *,
    width: int = 400,
    height: int = 80,
) -> str:
    """Sparkline SVG natif : polyline normalisée sur la fenêtre des valeurs."""
    if len(values) < 2:
        return (
            f'<svg width="{width}" height="{height}" '
            f'xmlns="http://www.w3.org/2000/svg">'
            '<text x="10" y="45" font-family="monospace" font-size="12">'
            "not enough data</text></svg>"
        )
    vmin = min(values)
    vmax = max(values)
    vrange = vmax - vmin if vmax != vmin else 1.0
    xs = [i * width / (len(values) - 1) for i in range(len(values))]
    ys = [height - (v - vmin) / vrange * height for v in values]
    points = " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(xs, ys, strict=True))
    del timestamps  # consommé uniquement côté rendering HTML externe
    return (
        f'<svg width="{width}" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<polyline fill="none" stroke="#2563eb" stroke-width="2" points="{points}" />'
        f"</svg>"
    )


def _render_stdout(stats: ReportStats) -> str:
    lines: list[str] = []
    lines.append(f"PnL report — since {stats.since_utc.isoformat()} (UTC)")
    if stats.latest is None:
        lines.append("no snapshots found on the window")
    else:
        lines.append(f"latest_total_usdc      : {stats.latest.total_usdc:.2f}")
        lines.append(f"latest_drawdown_pct    : {stats.latest.drawdown_pct:.2f}%")
        lines.append(f"max_drawdown_pct       : {stats.max_drawdown_pct:.2f}%")
        lines.append(f"total_usdc_delta       : {stats.total_usdc_delta:+.2f}")
        lines.append(f"snapshots_count        : {len(stats.snapshots)}")
    lines.append(f"open_positions_count   : {stats.open_positions_count}")
    lines.append(f"open_positions_notional: {stats.open_positions_notional:.2f}")
    lines.append(
        "orders_by_status       : "
        + ", ".join(f"{k}={v}" for k, v in sorted(stats.orders_by_status.items()))
    )
    return "\n".join(lines) + "\n"


def _render_csv(stats: ReportStats) -> str:
    buf: list[str] = []
    writer = csv.writer(_ListWriter(buf))
    writer.writerow(
        [
            "timestamp",
            "total_usdc",
            "drawdown_pct",
            "open_positions_count",
            "is_dry_run",
        ],
    )
    for s in stats.snapshots:
        writer.writerow(
            [
                s.timestamp.isoformat(),
                f"{s.total_usdc:.6f}",
                f"{s.drawdown_pct:.4f}",
                s.open_positions_count,
                int(s.is_dry_run),
            ],
        )
    return "".join(buf)


class _ListWriter:
    """Shim CSV: ``csv.writer`` needs a ``.write`` sink; we buffer into a list."""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def write(self, value: str) -> int:
        self._sink.append(value)
        return len(value)


def _render_html(stats: ReportStats) -> str:
    timestamps = [s.timestamp for s in stats.snapshots]
    values = [s.total_usdc for s in stats.snapshots]
    sparkline = _render_sparkline_svg(timestamps, values)
    rows_html = "\n".join(
        f"<tr><td>{html_module.escape(s.timestamp.isoformat())}</td>"
        f"<td>{s.total_usdc:.2f}</td>"
        f"<td>{s.drawdown_pct:.2f}%</td>"
        f"<td>{s.open_positions_count}</td>"
        f"<td>{'dry' if s.is_dry_run else 'real'}</td></tr>"
        for s in stats.snapshots
    )
    orders_summary = ", ".join(
        f"{html_module.escape(k)}={v}" for k, v in sorted(stats.orders_by_status.items())
    )
    latest_str = (
        f"{stats.latest.total_usdc:.2f} USDC (drawdown {stats.latest.drawdown_pct:.2f}%)"
        if stats.latest is not None
        else "no data"
    )
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>polycopy — PnL report</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 960px; margin: 2em auto; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1em; }}
  th, td {{ border: 1px solid #ddd; padding: 0.4em 0.6em; text-align: left; }}
  th {{ background: #f3f4f6; }}
  .metric {{ margin: 0.2em 0; }}
  .label {{ color: #6b7280; width: 18em; display: inline-block; }}
</style>
</head>
<body>
<h1>polycopy — PnL report</h1>
<p><em>since {html_module.escape(stats.since_utc.isoformat())} (UTC)</em></p>
<h2>Stats</h2>
<p class="metric"><span class="label">Latest total_usdc</span>{html_module.escape(latest_str)}</p>
<p class="metric"><span class="label">Max drawdown</span>{stats.max_drawdown_pct:.2f}%</p>
<p class="metric"><span class="label">Δ total_usdc</span>{stats.total_usdc_delta:+.2f}</p>
<p class="metric"><span class="label">Snapshots</span>{len(stats.snapshots)}</p>
<p class="metric"><span class="label">Open positions</span>
  {stats.open_positions_count} ({stats.open_positions_notional:.2f} USD notional)</p>
<p class="metric"><span class="label">Orders by status</span>{orders_summary or "∅"}</p>
<h2>Sparkline total_usdc</h2>
{sparkline}
<h2>Snapshots</h2>
<table>
<thead>
  <tr><th>timestamp</th><th>total_usdc</th><th>drawdown_pct</th>
      <th>open_positions</th><th>mode</th></tr>
</thead>
<tbody>
{rows_html}
</tbody>
</table>
</body>
</html>
"""


def run(args: argparse.Namespace) -> int:
    db_url = _sync_db_url(args.db or settings.database_url)
    engine = create_engine(db_url)
    try:
        with Session(engine) as session:
            stats = _collect_stats(
                session,
                since_days=args.since,
                include_dry_run=args.include_dry_run,
            )
    finally:
        engine.dispose()

    if args.output == "stdout":
        sys.stdout.write(_render_stdout(stats))
        return 0
    if args.output == "csv":
        sys.stdout.write(_render_csv(stats))
        return 0
    # html
    output_path = Path(args.output_file)
    output_path.write_text(_render_html(stats), encoding="utf-8")
    sys.stdout.write(f"wrote {output_path}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
