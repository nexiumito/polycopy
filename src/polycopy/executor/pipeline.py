"""Pipeline d'exécution d'un `OrderApproved` : metadata → POST → persist."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Literal

import structlog

from polycopy.executor.clob_metadata_client import ClobMetadataClient
from polycopy.executor.clob_write_client import ClobWriteClient
from polycopy.executor.dtos import (
    BuiltOrder,
    ExecutorAuthError,
    OrderResult,
)
from polycopy.executor.wallet_state_reader import WalletStateReader
from polycopy.storage.dtos import MyOrderDTO
from polycopy.storage.repositories import MyOrderRepository, MyPositionRepository
from polycopy.strategy.dtos import OrderApproved
from polycopy.strategy.gamma_client import GammaApiClient

if TYPE_CHECKING:
    from polycopy.config import Settings
    from polycopy.monitoring.dtos import Alert

log = structlog.get_logger(__name__)

_FIXED_MATH_DIVISOR = Decimal(10) ** 6

# Patterns d'erreur CLOB classifiés (spec §6.6). Tout ce qui n'est pas auth est traité
# comme validation à M3 (rejeté définitivement sans retry interne — le prochain
# OrderApproved tentera à nouveau si applicable).
_AUTH_ERROR_PATTERNS = ("api key", "L1 Request", "Unauthorized")


def _is_auth_error(error_msg: str) -> bool:
    lower = error_msg.lower()
    return any(pattern.lower() in lower for pattern in _AUTH_ERROR_PATTERNS)


def _round_to_tick(price: float, tick_size: float) -> float:
    """Arrondit `price` au multiple de `tick_size` le plus proche."""
    if tick_size <= 0:
        return price
    return round(price / tick_size) * tick_size


def _extract_fill(
    side: Literal["BUY", "SELL"],
    making_amount: str | None,
    taking_amount: str | None,
) -> tuple[float, float]:
    """Convertit `(makingAmount, takingAmount)` en `(shares_filled, fill_price_usd)`.

    Polymarket renvoie ces montants en fixed-math 6 décimales.
    - FOK BUY : maker=USDC versé, taker=shares reçus.
    - FOK SELL : maker=shares versés, taker=USDC reçu.
    """
    if making_amount is None or taking_amount is None:
        return 0.0, 0.0
    maker = Decimal(making_amount) / _FIXED_MATH_DIVISOR
    taker = Decimal(taking_amount) / _FIXED_MATH_DIVISOR
    if maker == 0 or taker == 0:
        return 0.0, 0.0
    if side == "BUY":
        shares_filled = float(taker)
        fill_price = float(maker / taker)
    else:
        shares_filled = float(maker)
        fill_price = float(taker / maker)
    return shares_filled, fill_price


async def execute_order(
    approved: OrderApproved,
    *,
    settings: "Settings",
    metadata_client: ClobMetadataClient,
    gamma_client: GammaApiClient,
    write_client: ClobWriteClient | None,
    wallet_state_reader: WalletStateReader,
    order_repo: MyOrderRepository,
    position_repo: MyPositionRepository,
    alerts_queue: "asyncio.Queue[Alert] | None" = None,
) -> None:
    """Exécute (ou simule) un `OrderApproved` reçu de la queue M2."""
    bound = log.bind(tx_hash=approved.tx_hash, condition_id=approved.condition_id)

    # 1) Fetch metadata + arrondi prix.
    tick_size = await metadata_client.get_tick_size(approved.asset_id)
    market = await gamma_client.get_market(approved.condition_id)
    neg_risk = market.neg_risk if market is not None else False
    price_rounded = _round_to_tick(approved.my_price, tick_size)

    built = BuiltOrder(
        token_id=approved.asset_id,
        side=approved.side,
        size=approved.my_size,
        price=price_rounded,
        tick_size=tick_size,
        neg_risk=neg_risk,
        order_type="FOK",
    )

    # 2) Branche dry-run.
    if settings.dry_run:
        await order_repo.insert(
            MyOrderDTO(
                source_tx_hash=approved.tx_hash,
                condition_id=approved.condition_id,
                asset_id=approved.asset_id,
                side=approved.side,
                size=approved.my_size,
                price=price_rounded,
                tick_size=tick_size,
                neg_risk=neg_risk,
                order_type="FOK",
                status="SIMULATED",
                simulated=True,
            ),
        )
        bound.info(
            "order_simulated",
            side=built.side,
            size=built.size,
            price=built.price,
            tick_size=tick_size,
            neg_risk=neg_risk,
        )
        return

    # 3) Branche réelle — garde-fou capital + double check + POST.
    if write_client is None:
        raise RuntimeError("execute_order called in real mode without write_client (bug)")
    wallet_state = await wallet_state_reader.get_state()
    prospective_cost = built.size * built.price
    if (
        wallet_state.total_position_value_usd + prospective_cost
        > wallet_state.available_capital_usd
    ):
        await order_repo.insert(
            MyOrderDTO(
                source_tx_hash=approved.tx_hash,
                condition_id=approved.condition_id,
                asset_id=approved.asset_id,
                side=approved.side,
                size=approved.my_size,
                price=price_rounded,
                tick_size=tick_size,
                neg_risk=neg_risk,
                order_type="FOK",
                # Placeholder ; corrigé par update_status() juste après.
                status="SIMULATED",
                simulated=False,
            ),
        )
        # Update vers REJECTED tout de suite (pas envoyé).
        recent = await order_repo.list_recent(limit=1)
        if recent:
            await order_repo.update_status(
                recent[0].id,
                "REJECTED",
                error_msg="capital_exceeded_at_executor",
            )
        bound.info(
            "order_rejected_at_executor",
            reason="capital_exceeded_at_executor",
            current_exposure=wallet_state.total_position_value_usd,
            prospective_cost=prospective_cost,
            available=wallet_state.available_capital_usd,
        )
        return

    if settings.dry_run:  # double check, defense in depth §2.3
        raise RuntimeError("dry_run flipped between checks (bug)")

    inserted = await order_repo.insert(
        MyOrderDTO(
            source_tx_hash=approved.tx_hash,
            condition_id=approved.condition_id,
            asset_id=approved.asset_id,
            side=approved.side,
            size=approved.my_size,
            price=price_rounded,
            tick_size=tick_size,
            neg_risk=neg_risk,
            order_type="FOK",
            status="SENT",
            simulated=False,
        ),
    )
    try:
        result: OrderResult = await write_client.post_order(built)
    except Exception as exc:  # noqa: BLE001
        await order_repo.update_status(inserted.id, "FAILED", error_msg=str(exc)[:240])
        bound.exception("executor_error", error=str(exc))
        _push_executor_error_alert(alerts_queue, approved, str(exc))
        return

    await _persist_result(
        result=result,
        order_id=inserted.id,
        approved=approved,
        order_repo=order_repo,
        position_repo=position_repo,
        bound_log=bound,
        alerts_queue=alerts_queue,
        settings=settings,
    )


async def _persist_result(
    *,
    result: OrderResult,
    order_id: int,
    approved: OrderApproved,
    order_repo: MyOrderRepository,
    position_repo: MyPositionRepository,
    bound_log: structlog.stdlib.BoundLogger,
    alerts_queue: "asyncio.Queue[Alert] | None" = None,
    settings: "Settings | None" = None,
) -> None:
    if not result.success:
        await order_repo.update_status(
            order_id,
            "REJECTED",
            error_msg=result.error_msg[:240],
            clob_order_id=result.clob_order_id,
        )
        if _is_auth_error(result.error_msg):
            bound_log.error("executor_auth_error", error=result.error_msg)
            raise ExecutorAuthError(result.error_msg)
        bound_log.info("order_rejected", error=result.error_msg)
        _push_executor_error_alert(alerts_queue, approved, result.error_msg)
        return

    if result.status == "matched":
        shares_filled, fill_price = _extract_fill(
            approved.side,
            result.making_amount,
            result.taking_amount,
        )
        await order_repo.update_status(
            order_id,
            "FILLED",
            clob_order_id=result.clob_order_id,
            taking_amount=result.taking_amount,
            making_amount=result.making_amount,
            transaction_hashes=result.transaction_hashes,
            filled_at=datetime.now(tz=UTC),
        )
        if shares_filled > 0:
            await position_repo.upsert_on_fill(
                condition_id=approved.condition_id,
                asset_id=approved.asset_id,
                side=approved.side,
                size_filled=shares_filled,
                fill_price=fill_price,
            )
        bound_log.info(
            "order_filled",
            clob_order_id=result.clob_order_id,
            shares_filled=shares_filled,
            fill_price=fill_price,
        )
        _maybe_push_large_fill_alert(
            alerts_queue,
            settings,
            approved,
            result.taking_amount,
        )
        return

    # status "live" ou "delayed" — peu probable pour FOK mais on couvre.
    await order_repo.update_status(
        order_id,
        "SENT",
        clob_order_id=result.clob_order_id,
    )
    bound_log.warning(
        "order_sent_unexpected_status",
        clob_order_id=result.clob_order_id,
        status=result.status,
    )


def _push_alert_nowait(
    alerts_queue: "asyncio.Queue[Alert] | None",
    alert: "Alert",
) -> None:
    """Non-bloquant : log+drop si la queue est pleine, no-op si queue absente."""
    if alerts_queue is None:
        return
    try:
        alerts_queue.put_nowait(alert)
    except asyncio.QueueFull:
        log.warning("alerts_queue_full", event=alert.event)


def _push_executor_error_alert(
    alerts_queue: "asyncio.Queue[Alert] | None",
    approved: OrderApproved,
    error_msg: str,
) -> None:
    if alerts_queue is None:
        return
    from polycopy.monitoring.dtos import Alert

    _push_alert_nowait(
        alerts_queue,
        Alert(
            level="ERROR",
            event="executor_error",
            body=(
                f"Executor error on tx {approved.tx_hash[:12]} — "
                f"condition {approved.condition_id[:12]} : {error_msg[:120]}"
            ),
            cooldown_key="executor_error",
        ),
    )


def _maybe_push_large_fill_alert(
    alerts_queue: "asyncio.Queue[Alert] | None",
    settings: "Settings | None",
    approved: OrderApproved,
    taking_amount: str | None,
) -> None:
    """Émet ``order_filled_large`` si taker (USD ou shares selon side) ≥ seuil.

    ``taking_amount`` est exprimé en fixed-math 6 décimales. Côté BUY il
    représente des *shares*, côté SELL des USDC — mais dans les deux cas une
    valeur élevée signale un fill atypique qui mérite une alerte.
    """
    if alerts_queue is None or settings is None or taking_amount is None:
        return
    try:
        taking_usd = Decimal(taking_amount) / _FIXED_MATH_DIVISOR
    except (InvalidOperation, TypeError):
        return
    if float(taking_usd) < settings.alert_large_order_usd_threshold:
        return
    from polycopy.monitoring.dtos import Alert

    _push_alert_nowait(
        alerts_queue,
        Alert(
            level="INFO",
            event="order_filled_large",
            body=(
                f"Large fill — tx {approved.tx_hash[:12]}, "
                f"side {approved.side}, taking_amount={float(taking_usd):.2f}."
            ),
            cooldown_key="order_filled_large",
        ),
    )
