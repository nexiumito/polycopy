#!/usr/bin/env python3
"""Helper diagnostic bot polycopy — vérifications boot + matin.

Cf. docs/night_test_runbook.md pour la procédure complète.

Usage ::

    # Après 60s de lancement (avant d'aller dormir) :
    python scripts/night_test_status.py --boot

    # Au matin, diagnostic complet :
    python scripts/night_test_status.py --full

    # Diagnostic court (stats DB uniquement) :
    python scripts/night_test_status.py --short

Exit codes :
    0 — tout OK (aucun warning/erreur)
    1 — warnings détectés (gates massifs, reconnects WS, etc.)
    2 — erreurs (process DOWN, kill switch, traceback)
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

try:
    from polycopy.config import Settings
except ImportError:
    print("[FATAL] polycopy not installed. Run: pip install -e '.[dev]'")
    sys.exit(2)


_PID_FILE = Path("/tmp/polycopy_night.pid")  # noqa: S108 — convention Unix PID file
_LOG_TAIL_LINES = 200  # Nb lignes log à analyser côté grep

RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
BLUE = "\033[94m"
BOLD = "\033[1m"
RESET = "\033[0m"


@dataclass
class CheckResult:
    name: str
    status: str  # "ok" | "warn" | "error" | "info"
    detail: str = ""
    lines: list[str] = field(default_factory=list)

    @property
    def color(self) -> str:
        return {
            "ok": GREEN,
            "warn": YELLOW,
            "error": RED,
            "info": BLUE,
        }.get(self.status, RESET)

    @property
    def glyph(self) -> str:
        return {"ok": "✓", "warn": "⚠", "error": "✗", "info": "ℹ"}.get(self.status, " ")


def _header(text: str) -> None:
    print(f"\n{BOLD}{BLUE}━━━ {text} ━━━{RESET}")


def _print(result: CheckResult) -> None:
    print(f"  {result.color}{result.glyph}{RESET} {BOLD}{result.name}{RESET}: {result.detail}")
    for line in result.lines:
        print(f"      {line}")


def _process_status() -> CheckResult:
    """Check bot process UP/DOWN via /tmp/polycopy_night.pid."""
    if not _PID_FILE.exists():
        return CheckResult(
            name="Process",
            status="warn",
            detail=f"PID file missing ({_PID_FILE}). Bot may be running without daemon wrapper.",
        )
    try:
        pid = int(_PID_FILE.read_text().strip())
    except (ValueError, OSError) as exc:
        return CheckResult(name="Process", status="error", detail=f"invalid PID file: {exc}")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return CheckResult(
            name="Process",
            status="error",
            detail=f"process PID={pid} NOT running (crashed or stopped).",
        )
    except PermissionError:
        # Process exists but we don't own it — still counts as alive.
        return CheckResult(name="Process", status="ok", detail=f"alive (PID={pid})")
    return CheckResult(name="Process", status="ok", detail=f"alive (PID={pid})")


def _dashboard_status(settings: Settings) -> CheckResult:
    """Check dashboard HTTP reachable."""
    if not settings.dashboard_enabled:
        return CheckResult(
            name="Dashboard", status="info", detail="disabled (DASHBOARD_ENABLED=false)"
        )
    url = f"http://{settings.dashboard_host}:{settings.dashboard_port}/healthz"
    try:
        with urlopen(url, timeout=3.0) as resp:  # noqa: S310 — localhost dashboard only
            if resp.status == 200:
                return CheckResult(name="Dashboard", status="ok", detail=f"200 OK at {url}")
            return CheckResult(
                name="Dashboard",
                status="warn",
                detail=f"HTTP {resp.status} at {url}",
            )
    except URLError as exc:
        return CheckResult(name="Dashboard", status="error", detail=f"unreachable: {exc.reason}")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(name="Dashboard", status="error", detail=f"unreachable: {exc}")


def _log_analysis(settings: Settings) -> list[CheckResult]:
    """Analyse fichier log polycopy (events boot + erreurs)."""
    log_file = Path(settings.log_file) if settings.log_file else None
    if not log_file or not log_file.exists():
        return [
            CheckResult(
                name="Log file",
                status="error",
                detail=f"not found ({log_file}). Bot ever started?",
            ),
        ]
    results: list[CheckResult] = []

    # Tail rapide pour les patterns. Inputs contrôlés (log_file vient de settings).
    try:
        tail = subprocess.run(  # noqa: S603
            ["tail", "-n", str(_LOG_TAIL_LINES), str(log_file)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            timeout=5.0,
        )
        log_tail = tail.stdout
    except (subprocess.SubprocessError, OSError):
        log_tail = log_file.read_text(errors="replace").splitlines()[-_LOG_TAIL_LINES:]
        log_tail = "\n".join(log_tail)  # type: ignore[assignment]

    # 1. Boot events.
    started = "discovery_started" in log_tail or "cli_boot_info" in log_tail
    results.append(
        CheckResult(
            name="Bot boot",
            status="ok" if started else "warn",
            detail="discovery_started detected" if started else "no boot events in tail",
        ),
    )

    # 2. Cycles completed.
    cycles = log_tail.count("discovery_cycle_completed")
    results.append(
        CheckResult(
            name="Discovery cycles",
            status="ok" if cycles > 0 else "warn",
            detail=f"{cycles} completed cycle(s) in log tail ({_LOG_TAIL_LINES} lines)",
        ),
    )

    # 3. Critical errors.
    critical_patterns = [
        ("kill_switch_triggered", "error"),
        ("Traceback", "error"),
        ("discovery_cycle_failed", "warn"),
        ("executor_error", "error"),
    ]
    for pattern, severity in critical_patterns:
        matches = [line for line in log_tail.splitlines() if pattern in line]
        if matches:
            results.append(
                CheckResult(
                    name=f"Pattern '{pattern}'",
                    status=severity,
                    detail=f"{len(matches)} match(es) in tail",
                    lines=matches[:3],  # premiers 3 exemples
                ),
            )

    # 4. WS connection status transitions (info seulement).
    ws_changes = log_tail.count("ws_connection_status_change")
    if ws_changes > 0:
        status = "warn" if ws_changes >= 5 else "info"
        verdict = "stability issue" if ws_changes >= 5 else "normal"
        results.append(
            CheckResult(
                name="WS reconnects",
                status=status,
                detail=f"{ws_changes} state transition(s) — {verdict}",
            ),
        )

    return results


def _db_stats(settings: Settings) -> list[CheckResult]:
    """SELECT-only stats DB pour diagnostic."""
    # sqlite+aiosqlite:///polycopy.db → polycopy.db
    db_url = settings.database_url
    match = re.match(r"sqlite(?:\+aiosqlite)?:///(.+)", db_url)
    if not match:
        return [
            CheckResult(
                name="DB",
                status="info",
                detail=f"non-sqlite URL ({db_url}), skipping DB stats",
            ),
        ]
    db_path = Path(match.group(1)).resolve()
    if not db_path.exists():
        return [CheckResult(name="DB", status="error", detail=f"not found ({db_path})")]

    results: list[CheckResult] = []
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            cur = conn.cursor()

            # Trades & orders.
            counts: dict[str, int] = {}
            for table in (
                "detected_trades",
                "my_orders",
                "my_positions",
                "pnl_snapshots",
                "trader_scores",
                "trader_events",
                "trader_daily_pnl",
            ):
                try:
                    # table name from hardcoded tuple above, pas d'input user.
                    (n,) = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()  # noqa: S608
                    counts[table] = int(n)
                except sqlite3.OperationalError:
                    counts[table] = -1  # table missing (migration non appliquée ?)

            results.append(
                CheckResult(
                    name="Trades / Orders / Positions",
                    status="ok" if counts["detected_trades"] > 0 else "info",
                    detail=(
                        f"detected={counts['detected_trades']} "
                        f"orders={counts['my_orders']} "
                        f"positions={counts['my_positions']}"
                    ),
                ),
            )

            # Scoring dual-compute healthy ?
            v1_count, v2_count = 0, 0
            try:
                rows = cur.execute(
                    "SELECT scoring_version, COUNT(*) FROM trader_scores GROUP BY scoring_version",
                ).fetchall()
                for version, count in rows:
                    if version == "v1":
                        v1_count = int(count)
                    elif version == "v2":
                        v2_count = int(count)
            except sqlite3.OperationalError:
                pass

            if v1_count > 0 and v2_count > 0:
                dual_status = "ok"
                detail = f"v1={v1_count} rows, v2={v2_count} rows (dual-compute healthy)"
            elif v1_count > 0 and v2_count == 0:
                dual_status = "warn"
                detail = (
                    f"v1={v1_count} rows, v2=0 (v2 not computing — "
                    "check SCORING_V2_SHADOW_DAYS or equity curves seed)"
                )
            elif v1_count == 0 and v2_count == 0:
                dual_status = "info"
                detail = "no scores yet — waiting 1st cycle completion"
            else:
                dual_status = "info"
                detail = f"v1={v1_count} v2={v2_count}"
            results.append(
                CheckResult(
                    name="Scoring v1/v2 rows",
                    status=dual_status,
                    detail=detail,
                ),
            )

            # Gate rejections.
            try:
                rows = cur.execute(
                    "SELECT reason, COUNT(*) FROM trader_events "
                    "WHERE event_type='gate_rejected' GROUP BY reason "
                    "ORDER BY COUNT(*) DESC LIMIT 5",
                ).fetchall()
                if rows:
                    total = sum(int(r[1]) for r in rows)
                    top = [f"{r[0][:60]} ({r[1]}×)" for r in rows[:3]]
                    status = "warn" if total > 100 else "info"
                    results.append(
                        CheckResult(
                            name="Gate rejections v2",
                            status=status,
                            detail=f"{total} rejected wallet(s) across {len(rows)} reasons",
                            lines=top,
                        ),
                    )
            except sqlite3.OperationalError:
                pass

            # Equity curves progression.
            try:
                rows = cur.execute(
                    "SELECT wallet_address, COUNT(*) FROM trader_daily_pnl "
                    "GROUP BY wallet_address ORDER BY COUNT(*) DESC LIMIT 5",
                ).fetchall()
                if rows:
                    results.append(
                        CheckResult(
                            name="Equity curves (trader_daily_pnl)",
                            status="ok",
                            detail=f"{len(rows)} wallet(s) tracked",
                            lines=[f"{r[0][:20]}…  {r[1]} days" for r in rows],
                        ),
                    )
            except sqlite3.OperationalError:
                pass

            # PnL latest snapshot.
            try:
                row = cur.execute(
                    "SELECT total_usdc, drawdown_pct, is_dry_run, timestamp "
                    "FROM pnl_snapshots ORDER BY timestamp DESC LIMIT 1",
                ).fetchone()
                if row:
                    total, dd, dry, ts = row
                    dd_status = (
                        "error"
                        if dd and float(dd) > 15
                        else ("warn" if dd and float(dd) > 5 else "ok")
                    )
                    results.append(
                        CheckResult(
                            name="Latest PnL snapshot",
                            status=dd_status,
                            detail=(
                                f"equity={float(total):.2f} USDC drawdown={float(dd):.2f}% "
                                f"{'(dry-run)' if dry else '(LIVE)'} at {ts}"
                            ),
                        ),
                    )
            except sqlite3.OperationalError:
                pass

            # Count cycles on 24h.
            try:
                (cycles_24h,) = cur.execute(
                    "SELECT COUNT(DISTINCT cycle_at) FROM trader_scores "
                    "WHERE cycle_at >= datetime('now', '-24 hours')",
                ).fetchone()
                results.append(
                    CheckResult(
                        name="Discovery cycles (24h)",
                        status="ok" if int(cycles_24h) > 0 else "info",
                        detail=f"{cycles_24h} distinct cycle(s) with ≥1 score written",
                    ),
                )
            except sqlite3.OperationalError:
                pass

    except sqlite3.Error as exc:
        results.append(CheckResult(name="DB query", status="error", detail=str(exc)))

    return results


def _run_boot(settings: Settings) -> int:
    _header("Boot check (lancement récent)")
    results: list[CheckResult] = []
    results.append(_process_status())
    results.append(_dashboard_status(settings))
    results.extend(_log_analysis(settings))
    worst = "ok"
    for r in results:
        _print(r)
        if r.status == "error":
            worst = "error"
        elif r.status == "warn" and worst != "error":
            worst = "warn"
    print()
    if worst == "error":
        print(f"{RED}{BOLD}✗ Boot check FAILED — investigate before sleeping{RESET}")
        return 2
    if worst == "warn":
        print(f"{YELLOW}{BOLD}⚠ Boot check WARNINGS — review before sleeping{RESET}")
        return 1
    print(f"{GREEN}{BOLD}✓ Boot OK — safe to sleep{RESET}")
    return 0


def _run_full(settings: Settings) -> int:
    _header("Process & Dashboard")
    results_proc = [_process_status(), _dashboard_status(settings)]
    for r in results_proc:
        _print(r)

    _header("Log analysis")
    results_logs = _log_analysis(settings)
    for r in results_logs:
        _print(r)

    _header("DB stats")
    results_db = _db_stats(settings)
    for r in results_db:
        _print(r)

    all_results = results_proc + results_logs + results_db
    worst = "ok"
    for r in all_results:
        if r.status == "error":
            worst = "error"
        elif r.status == "warn" and worst != "error":
            worst = "warn"
    print()
    if worst == "error":
        print(f"{RED}{BOLD}✗ Diagnostic : ERRORS detected{RESET}")
        return 2
    if worst == "warn":
        print(f"{YELLOW}{BOLD}⚠ Diagnostic : warnings (non-blocking){RESET}")
        return 1
    print(f"{GREEN}{BOLD}✓ Diagnostic : all OK{RESET}")
    return 0


def _run_short(settings: Settings) -> int:
    _header("DB stats")
    results = _db_stats(settings)
    for r in results:
        _print(r)
    worst = "ok"
    for r in results:
        if r.status == "error":
            worst = "error"
        elif r.status == "warn" and worst != "error":
            worst = "warn"
    return 0 if worst == "ok" else (1 if worst == "warn" else 2)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="polycopy night test diagnostic (cf. docs/night_test_runbook.md)",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--boot",
        action="store_true",
        help="Check boot (60s après lancement) : process UP, dashboard OK, 1er cycle démarre.",
    )
    group.add_argument(
        "--full",
        action="store_true",
        help="Diagnostic complet (matin) : process + logs + stats DB + PnL.",
    )
    group.add_argument(
        "--short",
        action="store_true",
        help="Stats DB uniquement (court).",
    )
    args = parser.parse_args()

    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001
        print(f"{RED}[FATAL] Settings invalid: {exc}{RESET}")
        sys.exit(2)

    # Default action : full.
    if not (args.boot or args.full or args.short):
        args.full = True

    if args.boot:
        sys.exit(_run_boot(settings))
    elif args.short:
        sys.exit(_run_short(settings))
    else:
        sys.exit(_run_full(settings))


if __name__ == "__main__":
    main()
