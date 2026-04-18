"""Client WebSocket pour le channel `market` du CLOB Polymarket (M11).

Remplace le polling HTTP ``/midpoint`` par un push cache in-memory pour
``SlippageChecker``. Read-only public : aucune creds L1/L2, pas d'auth, pas
de signature, pas de POST (cf. §3.1 + §10.3 spec M11).

Responsabilités :

- Connection persistante ``wss://ws-subscriptions-clob.polymarket.com/ws/market``.
- Souscription lazy channel `market` sur les ``token_id`` candidats.
- Cache ``{token_id: (mid_price, last_update_ts)}`` alimenté par les messages
  ``book`` / ``price_change`` / ``best_bid_ask`` / ``last_trade_price``.
- GC unsub après N secondes d'inactivité (default 300 s).
- Cap dur LRU (default 500 tokens) — anti-leak mémoire.
- Health check watchdog : si aucun message reçu en ``health_check_seconds``,
  statut transitionne `up → down` et déclenche un reconnect.
- Reconnect backoff exponentiel via ``tenacity``, métric
  ``ws_connection_status_change`` loggée à chaque transition.

Le client est lazy-instancié par ``StrategyOrchestrator`` uniquement si
``settings.strategy_clob_ws_enabled=True`` — sinon aucune connexion n'est
ouverte (fallback HTTP strict M2..M10).

Schéma des messages : capturé dans ``tests/fixtures/clob_ws_market_sample.jsonl``
(cf. ``scripts/capture_clob_ws_fixture.py`` pour rafraîchir).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

import structlog
import websockets
from pydantic import BaseModel, ConfigDict, Field
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:
    from polycopy.config import Settings

log = structlog.get_logger(__name__)
_tenacity_log = logging.getLogger(__name__)

_MAX_RECONNECT_ATTEMPTS = 10
_CACHE_STALE_SECONDS: float = 60.0
_GC_LOOP_INTERVAL_SECONDS: float = 30.0
_RECV_TIMEOUT_SECONDS: float = 30.0

WsStatus = Literal["up", "reconnecting", "down"]


# ------------------------------ DTOs ---------------------------------------


class _BookLevel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    price: str
    size: str


class BookSnapshot(BaseModel):
    """Snapshot orderbook complet envoyé au subscribe."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    event_type: Literal["book"]
    asset_id: str
    market: str
    bids: list[_BookLevel] = Field(default_factory=list)
    asks: list[_BookLevel] = Field(default_factory=list)
    timestamp: str
    hash: str | None = None


class _PriceChangeEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    asset_id: str
    price: str
    size: str
    side: Literal["BUY", "SELL"]
    best_bid: str | None = None
    best_ask: str | None = None
    hash: str | None = None


class PriceChangeEvent(BaseModel):
    """Message ``price_change`` — updates incrémentales orderbook."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    event_type: Literal["price_change"]
    market: str
    price_changes: list[_PriceChangeEntry] = Field(default_factory=list)
    timestamp: str


class LastTradePriceEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    event_type: Literal["last_trade_price"]
    asset_id: str
    market: str
    price: str
    side: Literal["BUY", "SELL"]
    size: str
    fee_rate_bps: str | None = None
    timestamp: str


class BestBidAskEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    event_type: Literal["best_bid_ask"]
    market: str
    asset_id: str
    best_bid: str
    best_ask: str
    spread: str | None = None
    timestamp: str


class MarketResolvedEvent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    event_type: Literal["market_resolved"]
    market: str
    assets_ids: list[str] = Field(default_factory=list)
    winning_asset_id: str | None = None
    winning_outcome: str | None = None
    timestamp: str | None = None


# ------------------------------ Client --------------------------------------


@dataclass
class _CacheEntry:
    """Valeur cache : mid-price et timestamp de la dernière mise à jour."""

    mid_price: float
    last_update_ts: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class ClobMarketWSClient:
    """Client WebSocket sur ``wss://ws-subscriptions-clob.polymarket.com/ws/market``.

    Le cycle de vie est piloté par ``run(stop_event)`` (tâche unique dans
    le TaskGroup du ``StrategyOrchestrator``). ``subscribe(token_id)`` et
    ``get_mid_price(token_id)`` sont appelés par ``SlippageChecker`` — thread-
    safe au sens asyncio (même event loop).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._url = settings.strategy_clob_ws_url
        self._max_subscribed = settings.strategy_clob_ws_max_subscribed
        self._inactivity_unsub_seconds = settings.strategy_clob_ws_inactivity_unsub_seconds
        self._health_check_seconds = settings.strategy_clob_ws_health_check_seconds

        self._cache: dict[str, _CacheEntry] = {}
        # OrderedDict pour LRU : la clé la plus récemment souscrite est en queue.
        self._subscribed: OrderedDict[str, datetime] = OrderedDict()
        # Pending subs non encore envoyés — poussés quand la connection est prête.
        self._pending_subs: set[str] = set()
        # ``Any`` car le type concret renvoyé par ``websockets.connect`` varie
        # entre les versions 12.x (``ClientConnection``) et la voie legacy
        # (``WebSocketClientProtocol``). L'API publique utilisée (``send`` /
        # ``recv`` / ``close``) est stable sur les deux.
        self._ws: Any | None = None
        self._status: WsStatus = "down"
        self._last_message_monotonic: float = time.monotonic()
        self._connected_event: asyncio.Event = asyncio.Event()

    # ------------------- API publique ----------------------------------

    @property
    def status(self) -> WsStatus:
        """État courant du WS (``up`` / ``reconnecting`` / ``down``)."""
        return self._status

    @property
    def cache_size(self) -> int:
        """Nombre d'entrées mid-price courantes (observabilité dashboard)."""
        return len(self._cache)

    @property
    def subscribed_count(self) -> int:
        """Nombre de tokens subscribés simultanément (observabilité)."""
        return len(self._subscribed)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Boucle principale : connect → listen → reconnect on error.

        Sort proprement quand ``stop_event`` est set (cancelle les tâches
        filles et ferme la WS).
        """
        gc_task = asyncio.create_task(self._gc_loop(stop_event), name="ws_gc_loop")
        watchdog_task = asyncio.create_task(
            self._watchdog_loop(stop_event),
            name="ws_watchdog_loop",
        )
        try:
            while not stop_event.is_set():
                try:
                    await self._connect_and_listen(stop_event)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("clob_ws_unexpected_error")
                    self._transition_status("down")
                    await asyncio.sleep(1.0)
        finally:
            for task in (gc_task, watchdog_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.close()
            self._transition_status("down")

    async def subscribe(self, token_id: str) -> None:
        """Lazy sub sur un ``token_id``. No-op si déjà subscribed.

        Enforce le cap dur ``max_subscribed`` via LRU unsub du plus ancien.
        Quand la WS est ``up``, envoie immédiatement ; sinon accumule dans
        ``_pending_subs`` pour réinjection au (re)connect.
        """
        now = datetime.now(tz=UTC)
        if token_id in self._subscribed:
            self._subscribed.move_to_end(token_id)
            self._subscribed[token_id] = now
            return
        await self._evict_lru_if_needed()
        self._subscribed[token_id] = now
        if self._ws is not None and self._status == "up":
            await self._send_subscribe([token_id])
        else:
            self._pending_subs.add(token_id)

    async def get_mid_price(self, token_id: str) -> float | None:
        """Retourne le mid_price depuis le cache, ou ``None`` si absent/stale."""
        entry = self._cache.get(token_id)
        if entry is None:
            return None
        age = (datetime.now(tz=UTC) - entry.last_update_ts).total_seconds()
        if age > _CACHE_STALE_SECONDS:
            return None
        # Touch LRU : un token activement consulté ne doit pas être GC.
        if token_id in self._subscribed:
            self._subscribed.move_to_end(token_id)
            self._subscribed[token_id] = datetime.now(tz=UTC)
        return entry.mid_price

    # ------------------- Connexion + listen -----------------------------

    async def _connect_and_listen(self, stop_event: asyncio.Event) -> None:
        """Un tour complet : (re)connect, re-sub, listen jusqu'à erreur."""
        try:
            await self._connect()
        except Exception:
            log.exception("clob_ws_connect_failed_all_retries")
            self._transition_status("down")
            await asyncio.sleep(5.0)
            return
        try:
            await self._flush_pending_subs()
            await self._listen_loop(stop_event)
        except (
            websockets.ConnectionClosed,
            websockets.ConnectionClosedError,
            websockets.ConnectionClosedOK,
        ):
            log.info("clob_ws_connection_closed")
            self._transition_status("reconnecting")
        finally:
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.close()
            self._ws = None
            self._connected_event.clear()

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(_MAX_RECONNECT_ATTEMPTS),
        before_sleep=before_sleep_log(_tenacity_log, logging.WARNING),
        retry=retry_if_exception_type((OSError, websockets.InvalidHandshake, TimeoutError)),
        reraise=True,
    )
    async def _connect(self) -> None:
        """Établit la connection WS avec ping_interval=10 (Polymarket expecte PING/10s)."""
        self._transition_status("reconnecting")
        ws = await websockets.connect(
            self._url,
            ping_interval=10,
            ping_timeout=20,
            close_timeout=5,
            max_size=2**20,
        )
        self._ws = ws
        self._last_message_monotonic = time.monotonic()
        self._connected_event.set()
        self._transition_status("up")
        log.info("clob_ws_connected", url=self._url)

    async def _listen_loop(self, stop_event: asyncio.Event) -> None:
        """Consomme les messages jusqu'à déconnection / stop_event."""
        assert self._ws is not None
        while not stop_event.is_set():
            try:
                raw = await asyncio.wait_for(
                    self._ws.recv(),
                    timeout=_RECV_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                # Pas de message pendant 30s — continuer, le watchdog décidera
                # si c'est anormal (seuil `health_check_seconds`).
                continue
            self._last_message_monotonic = time.monotonic()
            await self._handle_raw(raw)

    async def _handle_raw(self, raw: str | bytes) -> None:
        """Parse + dispatch un payload WS (objet unique ou array)."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw.strip() in {"PING", "PONG"}:
            # Polymarket peut renvoyer PONG en texte — pas de parse JSON.
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("clob_ws_invalid_json", preview=raw[:120])
            return
        messages: list[dict[str, Any]] = payload if isinstance(payload, list) else [payload]
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            event_type = msg.get("event_type")
            try:
                if event_type == "book":
                    self._apply_book(BookSnapshot.model_validate(msg))
                elif event_type == "price_change":
                    self._apply_price_change(PriceChangeEvent.model_validate(msg))
                elif event_type == "best_bid_ask":
                    self._apply_best_bid_ask(BestBidAskEvent.model_validate(msg))
                elif event_type == "last_trade_price":
                    self._apply_last_trade_price(LastTradePriceEvent.model_validate(msg))
                elif event_type == "market_resolved":
                    self._apply_market_resolved(MarketResolvedEvent.model_validate(msg))
                # tick_size_change et new_market ignorés pour le cache v1.
            except Exception:  # noqa: BLE001 — défense sur schema drift
                log.warning("clob_ws_parse_skipped", event_type=event_type)

    # ------------------- Application cache ------------------------------

    def _apply_book(self, book: BookSnapshot) -> None:
        mid = _compute_mid_from_book(book)
        if mid is not None:
            self._store(book.asset_id, mid)

    def _apply_price_change(self, event: PriceChangeEvent) -> None:
        for entry in event.price_changes:
            if entry.best_bid is None or entry.best_ask is None:
                continue
            try:
                bid = float(entry.best_bid)
                ask = float(entry.best_ask)
            except (TypeError, ValueError):
                continue
            if bid <= 0 or ask <= 0 or ask < bid:
                continue
            self._store(entry.asset_id, (bid + ask) / 2.0)

    def _apply_best_bid_ask(self, event: BestBidAskEvent) -> None:
        try:
            bid = float(event.best_bid)
            ask = float(event.best_ask)
        except (TypeError, ValueError):
            return
        if bid <= 0 or ask <= 0 or ask < bid:
            return
        self._store(event.asset_id, (bid + ask) / 2.0)

    def _apply_last_trade_price(self, event: LastTradePriceEvent) -> None:
        # ``last_trade_price`` ne porte pas best_bid/best_ask ; on l'utilise
        # uniquement si on n'a rien d'autre (fallback très léger).
        if event.asset_id in self._cache:
            return
        try:
            price = float(event.price)
        except (TypeError, ValueError):
            return
        if price <= 0:
            return
        self._store(event.asset_id, price)

    def _apply_market_resolved(self, event: MarketResolvedEvent) -> None:
        log.info("clob_ws_market_resolved", market=event.market)
        for asset_id in event.assets_ids:
            self._cache.pop(asset_id, None)
            self._subscribed.pop(asset_id, None)
            self._pending_subs.discard(asset_id)

    def _store(self, asset_id: str, mid: float) -> None:
        self._cache[asset_id] = _CacheEntry(mid_price=mid)
        if asset_id in self._subscribed:
            self._subscribed.move_to_end(asset_id)
            self._subscribed[asset_id] = datetime.now(tz=UTC)

    # ------------------- Subscribe / unsubscribe ------------------------

    async def _send_subscribe(self, token_ids: list[str]) -> None:
        assert self._ws is not None
        msg = {
            "type": "market",
            "assets_ids": token_ids,
            "custom_feature_enabled": True,
        }
        try:
            await self._ws.send(json.dumps(msg))
            log.info("clob_ws_subscribed", count=len(token_ids))
        except websockets.ConnectionClosed:
            self._pending_subs.update(token_ids)

    async def _send_unsubscribe(self, token_ids: list[str]) -> None:
        if self._ws is None or self._status != "up" or not token_ids:
            return
        msg = {"assets_ids": token_ids, "operation": "unsubscribe"}
        try:
            await self._ws.send(json.dumps(msg))
            log.info("clob_ws_unsubscribed", count=len(token_ids))
        except websockets.ConnectionClosed:
            pass

    async def _flush_pending_subs(self) -> None:
        pending = list(self._subscribed.keys())  # re-sub de tout après reconnect
        if not pending:
            return
        await self._send_subscribe(pending)
        self._pending_subs.clear()

    async def _evict_lru_if_needed(self) -> None:
        while len(self._subscribed) >= self._max_subscribed:
            oldest_id, _ = next(iter(self._subscribed.items()))
            self._subscribed.pop(oldest_id, None)
            self._cache.pop(oldest_id, None)
            self._pending_subs.discard(oldest_id)
            await self._send_unsubscribe([oldest_id])
            log.info("clob_ws_lru_evicted", token_id=oldest_id)

    # ------------------- Background loops -------------------------------

    async def _gc_loop(self, stop_event: asyncio.Event) -> None:
        """Unsub les token_ids inactifs depuis > inactivity_unsub_seconds."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=_GC_LOOP_INTERVAL_SECONDS,
                )
                return  # stop_event set → sortie
            except TimeoutError:
                pass
            now = datetime.now(tz=UTC)
            expired: list[str] = []
            for token_id, last_seen in list(self._subscribed.items()):
                if (now - last_seen).total_seconds() > self._inactivity_unsub_seconds:
                    expired.append(token_id)
            if expired:
                for token_id in expired:
                    self._subscribed.pop(token_id, None)
                    self._cache.pop(token_id, None)
                    self._pending_subs.discard(token_id)
                await self._send_unsubscribe(expired)
                log.info("clob_ws_gc_inactive", count=len(expired))

    async def _watchdog_loop(self, stop_event: asyncio.Event) -> None:
        """Watchdog : si pas de message reçu depuis health_check_seconds, force reconnect."""
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._health_check_seconds,
                )
                return
            except TimeoutError:
                pass
            if self._status != "up":
                continue
            idle = time.monotonic() - self._last_message_monotonic
            if idle > self._health_check_seconds * 2:
                log.warning("clob_ws_watchdog_stall", idle_seconds=round(idle, 1))
                self._transition_status("reconnecting")
                if self._ws is not None:
                    with contextlib.suppress(Exception):
                        await self._ws.close()

    # ------------------- Status transitions -----------------------------

    def _transition_status(self, new_status: WsStatus) -> None:
        if new_status == self._status:
            return
        previous = self._status
        self._status = new_status
        log.info(
            "ws_connection_status_change",
            previous=previous,
            status=new_status,
        )


# ------------------------------ Helpers ------------------------------------


def _compute_mid_from_book(book: BookSnapshot) -> float | None:
    """Calcule le mid depuis un snapshot orderbook (bids/asks en strings)."""
    try:
        best_bid = max((float(b.price) for b in book.bids), default=None)
        best_ask = min((float(a.price) for a in book.asks), default=None)
    except (TypeError, ValueError):
        return None
    if best_bid is None or best_ask is None:
        return None
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None
    return (best_bid + best_ask) / 2.0
