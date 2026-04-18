"""Poller async d'un unique wallet contre la Polymarket Data API.

M11 : génère un ``trade_id`` (uuid hex) par trade nouvellement inséré,
bind le contextvar structlog et logue le stage ``watcher_detected_ms``
(différence wall-clock entre ``trade.timestamp_utc`` et ingestion locale).
Le ``trade_id`` est propagé via ``DetectedTradeDTO.trade_id`` — nullable
pour compat tests M1..M10.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.repositories import DetectedTradeRepository
from polycopy.watcher.data_api_client import DataApiClient
from polycopy.watcher.dtos import TradeActivity

if TYPE_CHECKING:
    from polycopy.storage.repositories import TradeLatencyRepository

_BACKOFF_AFTER_ERROR_SECONDS: float = 5.0
_INITIAL_LOOKBACK_HOURS = 1


class WalletPoller:
    """Poll en boucle un wallet et persiste les trades nouveaux.

    Si une `out_queue` est fournie, push chaque `DetectedTradeDTO` nouvellement
    inséré (consommé par la strategy à M2). Compatible M1 (queue optionnelle).
    """

    def __init__(
        self,
        wallet_address: str,
        client: DataApiClient,
        repo: DetectedTradeRepository,
        interval_seconds: int,
        out_queue: asyncio.Queue[DetectedTradeDTO] | None = None,
        latency_repo: TradeLatencyRepository | None = None,
        instrumentation_enabled: bool = True,
    ) -> None:
        self._wallet = wallet_address.lower()
        self._client = client
        self._repo = repo
        self._interval = interval_seconds
        self._out_queue = out_queue
        self._latency_repo = latency_repo
        self._instrumentation_enabled = instrumentation_enabled
        self._log = structlog.get_logger(__name__).bind(wallet=self._wallet)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Boucle de polling jusqu'à ce que `stop_event` soit set ou tâche annulée."""
        last_ts = await self._repo.get_latest_timestamp(self._wallet)
        if last_ts is None:
            last_ts = datetime.now(tz=UTC) - timedelta(hours=_INITIAL_LOOKBACK_HOURS)
        elif last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=UTC)
        self._log.info(
            "poller_started",
            since=last_ts.isoformat(),
            interval=self._interval,
        )
        while not stop_event.is_set():
            try:
                await self._poll_once(last_ts)
                latest = await self._repo.get_latest_timestamp(self._wallet)
                if latest is not None:
                    last_ts = latest if latest.tzinfo else latest.replace(tzinfo=UTC)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._log.exception("poller_error")
                await self._sleep_or_stop(stop_event, _BACKOFF_AFTER_ERROR_SECONDS)
                continue
            await self._sleep_or_stop(stop_event, self._interval)
        self._log.info("poller_stopped")

    async def _poll_once(self, since: datetime) -> None:
        trades = await self._client.get_trades(self._wallet, since=since)
        for trade in trades:
            # Génère trade_id avant insertion pour pouvoir le propager dans le
            # DTO si insertion réussie.
            candidate_trade_id = uuid.uuid4().hex if self._instrumentation_enabled else None
            dto = self._to_dto(trade, trade_id=candidate_trade_id)
            inserted = await self._repo.insert_if_new(dto)
            if inserted:
                if candidate_trade_id is not None:
                    # Stage 1 : watcher_detected_ms = wall-clock onchain→local.
                    detected_ms = max(
                        0.0,
                        (datetime.now(tz=UTC) - dto.timestamp).total_seconds() * 1000.0,
                    )
                    structlog.contextvars.bind_contextvars(trade_id=candidate_trade_id)
                    self._log.info(
                        "trade_detected",
                        stage_name="watcher_detected_ms",
                        stage_duration_ms=round(detected_ms, 3),
                        tx_hash=trade.transaction_hash,
                        condition_id=trade.condition_id,
                        side=trade.side,
                        usdc_size=trade.usdc_size,
                        price=trade.price,
                    )
                    if self._latency_repo is not None:
                        await self._latency_repo.insert(
                            candidate_trade_id,
                            "watcher_detected_ms",
                            detected_ms,
                        )
                else:
                    self._log.info(
                        "trade_detected",
                        tx_hash=trade.transaction_hash,
                        condition_id=trade.condition_id,
                        side=trade.side,
                        usdc_size=trade.usdc_size,
                        price=trade.price,
                    )
                self._publish(dto)
            else:
                self._log.debug("trade_dedup_skipped", tx_hash=trade.transaction_hash)

    def _publish(self, dto: DetectedTradeDTO) -> None:
        if self._out_queue is None:
            return
        try:
            self._out_queue.put_nowait(dto)
        except asyncio.QueueFull:
            self._log.warning("strategy_queue_full", tx_hash=dto.tx_hash)

    def _to_dto(
        self,
        trade: TradeActivity,
        *,
        trade_id: str | None = None,
    ) -> DetectedTradeDTO:
        return DetectedTradeDTO(
            tx_hash=trade.transaction_hash,
            target_wallet=self._wallet,
            condition_id=trade.condition_id,
            asset_id=trade.asset,
            side=trade.side,
            size=trade.size,
            usdc_size=trade.usdc_size,
            price=trade.price,
            timestamp=datetime.fromtimestamp(trade.timestamp, tz=UTC),
            outcome=trade.outcome,
            slug=trade.slug,
            raw_json=trade.model_dump(by_alias=True),
            trade_id=trade_id,
        )

    @staticmethod
    async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        except TimeoutError:
            return
