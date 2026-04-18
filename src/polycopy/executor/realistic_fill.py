"""Algorithme M8 de simulation FOK taker à partir d'un orderbook /book.

Pure function ``simulate_fill(order, book, *, allow_partial)`` — aucun I/O,
aucune dépendance config. Testable de façon exhaustive (cf.
``tests/unit/test_realistic_fill.py`` + property test ``hypothesis``).

Invariants :
- BUY consomme les **asks** (prix croissant, on paie le meilleur d'abord).
- SELL consomme les **bids** (prix décroissant, on vend au meilleur d'abord).
- ``Decimal`` pour les calculs (évite les erreurs d'arrondi sur 20+ niveaux).
- ``float`` uniquement pour la persistance DB / logs (cf. ``RealisticFillResult``).
- FOK strict : ``allow_partial=False`` + book insuffisant → ``REJECTED``.
- ``allow_partial=True`` + book insuffisant → fill partiel sur ce qui est dispo.
- Book vide (asks=[] pour BUY, bids=[] pour SELL) → ``REJECTED`` ``empty_book``.
"""

from __future__ import annotations

from decimal import Decimal

from polycopy.executor.dtos import Orderbook, RealisticFillResult
from polycopy.strategy.dtos import OrderApproved


def simulate_fill(
    order: OrderApproved,
    book: Orderbook,
    *,
    allow_partial: bool,
) -> RealisticFillResult:
    """Simule un fill FOK taker à partir de la profondeur orderbook.

    Args:
        order: ordre approuvé par le pipeline strategy (size + side).
        book: snapshot orderbook CLOB capturé via ``GET /book``.
        allow_partial: si ``True``, accepte un fill partiel quand le book est
            insuffisant ; sinon rejette (FOK strict, cohérent M3 live).

    Returns:
        ``RealisticFillResult`` — ``status='SIMULATED'`` avec ``avg_fill_price``
        pondéré, ou ``status='REJECTED'`` avec ``reason`` (``empty_book`` ou
        ``insufficient_liquidity``).
    """
    requested = Decimal(str(order.my_size))
    if order.side == "BUY":
        levels = sorted(book.asks, key=lambda lvl: lvl.price)
    else:
        levels = sorted(book.bids, key=lambda lvl: -lvl.price)

    if not levels:
        return RealisticFillResult(
            status="REJECTED",
            reason="empty_book",
            requested_size=order.my_size,
            shortfall=order.my_size,
        )

    remaining = requested
    consumed_usd = Decimal("0")
    consumed_shares = Decimal("0")
    levels_touched = 0

    for lvl in levels:
        if remaining <= 0:
            break
        fill_size = min(remaining, lvl.size)
        consumed_usd += fill_size * lvl.price
        consumed_shares += fill_size
        remaining -= fill_size
        levels_touched += 1

    shortfall = float(remaining) if remaining > 0 else 0.0

    if remaining > 0 and not allow_partial:
        return RealisticFillResult(
            status="REJECTED",
            reason="insufficient_liquidity",
            requested_size=order.my_size,
            depth_consumed_shares=float(consumed_shares),
            depth_consumed_levels=levels_touched,
            shortfall=shortfall,
        )

    if consumed_shares <= 0:
        # Cas dégénéré : toutes les sizes des levels sont nulles.
        return RealisticFillResult(
            status="REJECTED",
            reason="empty_book",
            requested_size=order.my_size,
            depth_consumed_levels=levels_touched,
            shortfall=order.my_size,
        )

    avg_price = float(consumed_usd / consumed_shares)
    return RealisticFillResult(
        status="SIMULATED",
        reason=None,
        requested_size=order.my_size,
        filled_size=float(consumed_shares),
        avg_fill_price=avg_price,
        depth_consumed_shares=float(consumed_shares),
        depth_consumed_levels=levels_touched,
        shortfall=shortfall,
    )
