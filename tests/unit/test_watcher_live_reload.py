"""Tests live-reload du ``WatcherOrchestrator`` (M5_ter).

14 cas couvrent : boot froid, promotion pendant run, transitions M5_bis
(active↔sell_only, sell_only→shadow, active→shadow), blacklist double-check,
no-op spam, stop_event mid-cycle, DB failure retry, pinned preservation,
coexistence DISCOVERY+blacklist, non-régression interval élevé, cancel
multiple.

Stratégie : pour éviter les races SQLite in-memory (connections partagées
entre pollers + hook + reload fetch), on mocke directement
``TargetTraderRepository.list_wallets_to_poll`` avec un itérateur contrôlé
par cycle. ``DataApiClient.get_trades`` → ``[]`` pour des pollers idle.
``_sleep_or_stop`` est monkeypatché sur un compteur qui stoppe après N
cycles — évite de relâcher la validation Pydantic ``Field(ge=30)`` sur
le reload interval.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from polycopy.config import Settings
from polycopy.storage.models import TargetTrader
from polycopy.storage.repositories import (
    DetectedTradeRepository,
    TargetTraderRepository,
)
from polycopy.watcher import data_api_client as data_api_client_module
from polycopy.watcher.orchestrator import WatcherOrchestrator


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "_env_file": None,
        "poll_interval_seconds": 1,
        "watcher_reload_interval_seconds": 30,
        "latency_instrumentation_enabled": False,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _make_trader(wallet: str, *, status: str = "active", pinned: bool = False) -> TargetTrader:
    return TargetTrader(
        wallet_address=wallet.lower(),
        active=True,
        status=status,
        pinned=pinned,
    )


class _ScriptedRepo:
    """Remplace ``list_wallets_to_poll`` par un script de cycles.

    Chaque appel retourne la liste suivante de ``TargetTrader`` seedés en
    mémoire. Si l'entrée du script est une exception, elle est raise.
    ``blacklist`` reçu est enregistré pour assertion.
    """

    def __init__(self, cycles: list[list[TargetTrader] | Exception]) -> None:
        self._cycles = cycles
        self._idx = 0
        self.blacklist_calls: list[list[str] | None] = []

    async def __call__(
        self,
        *,
        blacklist: list[str] | None = None,
    ) -> list[TargetTrader]:
        entry = self._cycles[min(self._idx, len(self._cycles) - 1)]
        self._idx += 1
        self.blacklist_calls.append(blacklist)
        if isinstance(entry, Exception):
            raise entry
        # Simule le double-check Python-side de list_wallets_to_poll.
        if blacklist:
            blacklist_lc = {w.lower() for w in blacklist}
            return [t for t in entry if t.wallet_address.lower() not in blacklist_lc]
        return entry


def _install_scripted_repo(
    monkeypatch: pytest.MonkeyPatch,
    cycles: list[list[TargetTrader] | Exception],
) -> _ScriptedRepo:
    scripted = _ScriptedRepo(cycles)
    monkeypatch.setattr(TargetTraderRepository, "list_wallets_to_poll", scripted)
    return scripted


class _FastSleep:
    """Compteur qui stoppe l'orchestrator après ``total_cycles`` appels."""

    def __init__(self, total_cycles: int) -> None:
        self.count = 0
        self.total = total_cycles

    async def __call__(self, stop_event: asyncio.Event, seconds: float) -> bool:
        del seconds
        self.count += 1
        await asyncio.sleep(0)
        if self.count >= self.total:
            stop_event.set()
            return True
        return stop_event.is_set()


def _install_fast_sleep(monkeypatch: pytest.MonkeyPatch, total_cycles: int) -> _FastSleep:
    fast = _FastSleep(total_cycles=total_cycles)
    monkeypatch.setattr(WatcherOrchestrator, "_sleep_or_stop", staticmethod(fast))
    return fast


async def _empty_get_trades(
    self: object,
    wallet: str,
    since: object = None,
    limit: int = 100,
) -> list[object]:
    del self, wallet, since, limit
    return []


async def _empty_latest_timestamp(self: object, wallet: str) -> None:
    del self, wallet


async def _noop_insert_if_new(self: object, trade: object) -> bool:
    del self, trade
    return True


@pytest.fixture(autouse=True)
def _stub_network_and_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Evite les vrais appels réseau (Data API) et les opérations DB
    concurrentes (SQLite ``:memory:`` + StaticPool ne tolère pas des
    sessions concurrentes — M5_ter teste la logique orchestrator, pas
    les pollers)."""
    monkeypatch.setattr(
        data_api_client_module.DataApiClient,
        "get_trades",
        _empty_get_trades,
    )
    monkeypatch.setattr(
        DetectedTradeRepository,
        "get_latest_timestamp",
        _empty_latest_timestamp,
    )
    monkeypatch.setattr(
        DetectedTradeRepository,
        "insert_if_new",
        _noop_insert_if_new,
    )


def _reload_cycle_logs(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry.get("event") == "watcher_reload_cycle"]


def _reload_noop_logs(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry.get("event") == "watcher_reload_cycle_noop"]


# --- 1. Boot froid — 3 actives démarrent au 1er cycle -------------------------


@pytest.mark.asyncio
async def test_boot_cold_starts_three_pollers(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traders = [_make_trader(w) for w in ("0xaaa", "0xbbb", "0xccc")]
    _install_scripted_repo(monkeypatch, [traders])
    _install_fast_sleep(monkeypatch, total_cycles=1)

    orchestrator = WatcherOrchestrator(session_factory, _settings())
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    cycles = _reload_cycle_logs(logs)
    assert len(cycles) == 1
    assert cycles[0]["added"] == 3
    assert cycles[0]["removed"] == 0
    assert cycles[0]["total"] == 3
    assert set(cycles[0]["added_wallets"]) == {"0xaaa", "0xbbb", "0xccc"}


# --- 2. Promote après boot détecté au cycle suivant --------------------------


@pytest.mark.asyncio
async def test_promote_shadow_to_active_detected_next_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle1 = [_make_trader("0xaaa"), _make_trader("0xbbb")]
    cycle2 = [*cycle1, _make_trader("0xnew")]
    _install_scripted_repo(monkeypatch, [cycle1, cycle2])
    _install_fast_sleep(monkeypatch, total_cycles=2)

    orchestrator = WatcherOrchestrator(session_factory, _settings())
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    cycles = _reload_cycle_logs(logs)
    assert len(cycles) == 2
    assert cycles[0]["added"] == 2
    assert cycles[1]["added"] == 1
    assert cycles[1]["added_wallets"] == ["0xnew"]
    assert cycles[1]["removed"] == 0


# --- 3. active → sell_only = no-op (watcher continue de poller) --------------


@pytest.mark.asyncio
async def test_active_to_sell_only_is_noop(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle1 = [_make_trader("0xaaa", status="active")]
    cycle2 = [_make_trader("0xaaa", status="sell_only")]
    _install_scripted_repo(monkeypatch, [cycle1, cycle2])
    _install_fast_sleep(monkeypatch, total_cycles=2)

    orchestrator = WatcherOrchestrator(session_factory, _settings())
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    cycles = _reload_cycle_logs(logs)
    noops = _reload_noop_logs(logs)
    # Un seul info log (le boot) + au moins un noop (cycle 2 vit la même set).
    assert len(cycles) == 1
    assert cycles[0]["added"] == 1
    assert len(noops) >= 1
    assert all(entry["total"] == 1 for entry in noops)


# --- 4. sell_only → active = no-op (poller déjà présent) ---------------------


@pytest.mark.asyncio
async def test_sell_only_to_active_is_noop(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle1 = [_make_trader("0xaaa", status="sell_only")]
    cycle2 = [_make_trader("0xaaa", status="active")]
    _install_scripted_repo(monkeypatch, [cycle1, cycle2])
    _install_fast_sleep(monkeypatch, total_cycles=2)

    orchestrator = WatcherOrchestrator(session_factory, _settings())
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    cycles = _reload_cycle_logs(logs)
    assert len(cycles) == 1
    assert cycles[0]["added_wallets"] == ["0xaaa"]


# --- 5. sell_only → shadow = cancel poller -----------------------------------


@pytest.mark.asyncio
async def test_sell_only_to_shadow_cancels_poller(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle1 = [_make_trader("0xaaa"), _make_trader("0xbbb", status="sell_only")]
    cycle2 = [_make_trader("0xaaa")]
    _install_scripted_repo(monkeypatch, [cycle1, cycle2])
    _install_fast_sleep(monkeypatch, total_cycles=2)

    orchestrator = WatcherOrchestrator(session_factory, _settings())
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    cycles = _reload_cycle_logs(logs)
    assert len(cycles) == 2
    assert cycles[1]["added"] == 0
    assert cycles[1]["removed"] == 1
    assert cycles[1]["removed_wallets"] == ["0xbbb"]


# --- 6. active → shadow (demote T4) = cancel poller --------------------------


@pytest.mark.asyncio
async def test_active_to_shadow_cancels_poller(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle1 = [_make_trader("0xaaa")]
    cycle2: list[TargetTrader] = []
    _install_scripted_repo(monkeypatch, [cycle1, cycle2])
    _install_fast_sleep(monkeypatch, total_cycles=2)

    orchestrator = WatcherOrchestrator(session_factory, _settings())
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    cycles = _reload_cycle_logs(logs)
    assert len(cycles) == 2
    assert cycles[1]["removed"] == 1
    assert cycles[1]["removed_wallets"] == ["0xaaa"]


# --- 7. Blacklist env : wallet jamais instancié ------------------------------


@pytest.mark.asyncio
async def test_blacklist_env_wallet_never_polled(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle1 = [_make_trader("0xaaa"), _make_trader("0xbad")]
    scripted = _install_scripted_repo(monkeypatch, [cycle1])
    _install_fast_sleep(monkeypatch, total_cycles=1)

    settings = _settings(blacklisted_wallets=["0xbad"])
    orchestrator = WatcherOrchestrator(session_factory, settings)
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    cycles = _reload_cycle_logs(logs)
    assert cycles[0]["added_wallets"] == ["0xaaa"]
    assert "0xbad" not in cycles[0]["added_wallets"]
    # Vérifie que la blacklist a bien été passée au repository.
    assert scripted.blacklist_calls[0] == ["0xbad"]


# --- 8. 10 cycles consécutifs no-op : aucun spam info -----------------------


@pytest.mark.asyncio
async def test_ten_cycles_noop_no_info_spam(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_state = [_make_trader("0xaaa")]
    _install_scripted_repo(monkeypatch, [cycle_state])
    _install_fast_sleep(monkeypatch, total_cycles=10)

    orchestrator = WatcherOrchestrator(session_factory, _settings())
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    cycles = _reload_cycle_logs(logs)
    noops = _reload_noop_logs(logs)
    # Invariant "no info spam" : uniquement le cycle boot émet un info log.
    # Les cycles suivants sont tous des noops (debug) — count exact non
    # testé pour robustesse contre le plugin scheduling pytest-asyncio.
    assert len(cycles) == 1
    assert len(noops) >= 1
    assert all(entry["total"] == 1 for entry in noops)


# --- 9. stop_event.set() mid-run → exit propre ------------------------------


@pytest.mark.asyncio
async def test_stop_event_mid_cycle_clean_exit(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traders = [_make_trader(w) for w in ("0xaaa", "0xbbb", "0xccc")]
    _install_scripted_repo(monkeypatch, [traders])
    _install_fast_sleep(monkeypatch, total_cycles=1)

    orchestrator = WatcherOrchestrator(session_factory, _settings())
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    events = [entry.get("event") for entry in logs]
    assert "watcher_started" in events
    assert "watcher_stopped" in events
    stopped = next(e for e in logs if e.get("event") == "watcher_stopped")
    assert stopped["final_pollers"] == 3


# --- 10. list_wallets_to_poll raise → log warning + retry --------------------


@pytest.mark.asyncio
async def test_list_wallets_to_poll_failure_is_logged_and_retried(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycles_script = [
        RuntimeError("simulated DB lock"),
        [_make_trader("0xaaa")],
    ]
    _install_scripted_repo(monkeypatch, cycles_script)
    _install_fast_sleep(monkeypatch, total_cycles=2)

    orchestrator = WatcherOrchestrator(session_factory, _settings())
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    events = [entry.get("event") for entry in logs]
    assert "watcher_reload_failed" in events
    cycles = _reload_cycle_logs(logs)
    assert any(entry["added"] == 1 and entry["added_wallets"] == ["0xaaa"] for entry in cycles)


# --- 11. Pinned reste polled (invariant preservation) -----------------------


@pytest.mark.asyncio
async def test_pinned_wallet_always_polled(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned_trader = _make_trader("0xpinned", status="pinned", pinned=True)
    # Même état retourné à tous les cycles → pinned conservé.
    _install_scripted_repo(monkeypatch, [[pinned_trader]])
    _install_fast_sleep(monkeypatch, total_cycles=5)

    orchestrator = WatcherOrchestrator(session_factory, _settings())
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    cycles = _reload_cycle_logs(logs)
    noops = _reload_noop_logs(logs)
    assert len(cycles) == 1
    assert cycles[0]["added_wallets"] == ["0xpinned"]
    assert all(entry["total"] == 1 for entry in noops)


# --- 12. BLACKLIST double-check (DB blacklisted + env var blacklist) --------


@pytest.mark.asyncio
async def test_blacklisted_status_and_env_double_check(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dans la vraie DB, `status='blacklisted'` est filtré côté SQL. Ici
    # le scripted repo ne retourne jamais un `blacklisted` (simule la
    # clause IN). On vérifie que l'env filtre le 2ᵉ (0xbad2 — race).
    cycle1 = [_make_trader("0xaaa"), _make_trader("0xbad2")]
    _install_scripted_repo(monkeypatch, [cycle1])
    _install_fast_sleep(monkeypatch, total_cycles=1)

    settings = _settings(blacklisted_wallets=["0xbad2"])
    orchestrator = WatcherOrchestrator(session_factory, settings)
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    cycles = _reload_cycle_logs(logs)
    assert cycles[0]["added_wallets"] == ["0xaaa"]
    assert "0xbad2" not in cycles[0]["added_wallets"]


# --- 13. Reload interval 3600 → boot-parity (1 cycle rapide) ----------------


@pytest.mark.asyncio
async def test_long_reload_interval_single_boot_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_scripted_repo(monkeypatch, [[_make_trader("0xaaa")]])
    _install_fast_sleep(monkeypatch, total_cycles=1)

    settings = _settings(watcher_reload_interval_seconds=3600)
    orchestrator = WatcherOrchestrator(session_factory, settings)
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    started = next(e for e in logs if e.get("event") == "watcher_started")
    assert started["reload_interval"] == 3600
    cycles = _reload_cycle_logs(logs)
    assert len(cycles) == 1
    assert cycles[0]["added"] == 1


# --- 14. 2 wallets retirés simultanément → gather absorbe ------------------


@pytest.mark.asyncio
async def test_multiple_pollers_cancelled_same_cycle(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle1 = [_make_trader(w) for w in ("0xaaa", "0xbbb", "0xccc")]
    cycle2 = [_make_trader("0xaaa")]
    _install_scripted_repo(monkeypatch, [cycle1, cycle2])
    _install_fast_sleep(monkeypatch, total_cycles=2)

    orchestrator = WatcherOrchestrator(session_factory, _settings())
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    cycles = _reload_cycle_logs(logs)
    assert len(cycles) == 2
    assert cycles[1]["removed"] == 2
    assert set(cycles[1]["removed_wallets"]) == {"0xbbb", "0xccc"}
    assert cycles[1]["total"] == 1


# --- Security grep — aucun secret loggé dans les events watcher_* ----------


@pytest.mark.asyncio
async def test_no_secret_leak_in_watcher_logs(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Régression sécu : les events ``watcher_*`` ne doivent contenir aucun
    marker de secret (private key, telegram token, TOTP)."""
    structlog.reset_defaults()
    _install_scripted_repo(monkeypatch, [[_make_trader("0xaaa")]])
    _install_fast_sleep(monkeypatch, total_cycles=3)

    settings = _settings(
        polymarket_private_key="0xSECRETSECRETSECRET",
        telegram_bot_token="TGSECRETTOKEN",
        remote_control_totp_secret="JBSWY3DPEHPK3PXP",
    )
    orchestrator = WatcherOrchestrator(session_factory, settings)
    stop = asyncio.Event()

    with capture_logs() as logs:
        await orchestrator.run_forever(stop)

    secret_markers = ("0xSECRETSECRETSECRET", "TGSECRETTOKEN", "JBSWY3DPEHPK3PXP")
    for entry in logs:
        serialized = str(entry)
        for marker in secret_markers:
            assert marker not in serialized
