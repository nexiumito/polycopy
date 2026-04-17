"""Poller async d'un unique wallet contre la Polymarket Data API."""

import asyncio
from datetime import UTC, datetime, timedelta

import structlog

from polycopy.storage.dtos import DetectedTradeDTO
from polycopy.storage.repositories import DetectedTradeRepository
from polycopy.watcher.data_api_client import DataApiClient
from polycopy.watcher.dtos import TradeActivity

_BACKOFF_AFTER_ERROR_SECONDS: float = 5.0
_INITIAL_LOOKBACK_HOURS = 1


class WalletPoller:
    """Poll en boucle un wallet et persiste les trades nouveaux."""

    def __init__(
        self,
        wallet_address: str,
        client: DataApiClient,
        repo: DetectedTradeRepository,
        interval_seconds: int,
    ) -> None:
        self._wallet = wallet_address.lower()
        self._client = client
        self._repo = repo
        self._interval = interval_seconds
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
            inserted = await self._repo.insert_if_new(self._to_dto(trade))
            if inserted:
                self._log.info(
                    "trade_detected",
                    tx_hash=trade.transaction_hash,
                    condition_id=trade.condition_id,
                    side=trade.side,
                    usdc_size=trade.usdc_size,
                    price=trade.price,
                )
            else:
                self._log.debug("trade_dedup_skipped", tx_hash=trade.transaction_hash)

    def _to_dto(self, trade: TradeActivity) -> DetectedTradeDTO:
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
