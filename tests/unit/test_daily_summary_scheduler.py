"""Tests du ``DailySummaryScheduler`` : next tick + DST + send."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from polycopy.config import Settings
from polycopy.monitoring.alert_renderer import AlertRenderer
from polycopy.monitoring.daily_summary_scheduler import (
    DailySummaryScheduler,
    compute_next_summary_at,
)


def test_next_summary_at_today_future_utc() -> None:
    now = datetime(2026, 4, 18, 8, 0, tzinfo=UTC)
    nxt = compute_next_summary_at(now, 9, ZoneInfo("UTC"))
    assert nxt == datetime(2026, 4, 18, 9, 0, tzinfo=UTC)


def test_next_summary_at_today_past_rolls_tomorrow() -> None:
    now = datetime(2026, 4, 18, 10, 0, tzinfo=UTC)
    nxt = compute_next_summary_at(now, 9, ZoneInfo("UTC"))
    assert nxt == datetime(2026, 4, 19, 9, 0, tzinfo=UTC)


def test_next_summary_at_handles_europe_paris_summer() -> None:
    # 12:00 UTC = 14:00 Paris (CEST, UTC+2 en été). Cible 09:00 Paris → demain 07:00 UTC.
    now = datetime(2026, 6, 18, 12, 0, tzinfo=UTC)
    nxt = compute_next_summary_at(now, 9, ZoneInfo("Europe/Paris"))
    assert nxt == datetime(2026, 6, 19, 7, 0, tzinfo=UTC)


def test_next_summary_at_handles_europe_paris_winter() -> None:
    # Hiver : Paris UTC+1. 07:00 UTC = 08:00 Paris. Cible 09:00 Paris → 08:00 UTC même jour.
    now = datetime(2026, 1, 18, 7, 0, tzinfo=UTC)
    nxt = compute_next_summary_at(now, 9, ZoneInfo("Europe/Paris"))
    assert nxt == datetime(2026, 1, 18, 8, 0, tzinfo=UTC)


def test_next_summary_at_naive_input_treated_as_utc() -> None:
    now_naive = datetime(2026, 4, 18, 8, 0)
    nxt = compute_next_summary_at(now_naive, 9, ZoneInfo("UTC"))
    assert nxt == datetime(2026, 4, 18, 9, 0, tzinfo=UTC)


def test_next_summary_at_spring_forward_dst() -> None:
    # Paris DST spring forward 2026 : dimanche 29 mars à 02:00 → saut à 03:00.
    # Now = 28 mars 23:00 UTC (= 29 mars 00:00 CET). Cible 04:00 Paris → 29 mars 02:00 UTC.
    now = datetime(2026, 3, 28, 23, 0, tzinfo=UTC)
    nxt = compute_next_summary_at(now, 4, ZoneInfo("Europe/Paris"))
    # 29 mars 04:00 Paris (CEST après saut) = 02:00 UTC (UTC+2).
    assert nxt == datetime(2026, 3, 29, 2, 0, tzinfo=UTC)


# --- Scheduler runtime tests ------------------------------------------------


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        telegram_bot_token="123:abc",
        telegram_chat_id="42",
        telegram_daily_summary=True,
        tg_daily_summary_hour=9,
        tg_daily_summary_timezone="UTC",
    )


class _FakeDispatcher:
    @property
    def counts_since_boot(self) -> dict[str, int]:
        return {"order_filled_large": 3}


@pytest.mark.asyncio
async def test_daily_noop_when_telegram_disabled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    tg = AsyncMock()
    tg.enabled = False
    tg.send = AsyncMock(return_value=True)
    scheduler = DailySummaryScheduler(
        session_factory,
        tg,
        AlertRenderer(),
        settings,
        _FakeDispatcher(),  # type: ignore[arg-type]
    )
    stop = asyncio.Event()
    await scheduler.run(stop)  # retour immédiat
    tg.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_daily_stop_event_short_circuits_wait(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings()
    tg = AsyncMock()
    tg.enabled = True
    tg.send = AsyncMock(return_value=True)
    scheduler = DailySummaryScheduler(
        session_factory,
        tg,
        AlertRenderer(),
        settings,
        _FakeDispatcher(),  # type: ignore[arg-type]
    )
    stop = asyncio.Event()
    stop.set()
    # Le scheduler doit détecter stop_event et quitter sans envoyer.
    await scheduler.run(stop)


@pytest.mark.asyncio
async def test_daily_send_summary_integration(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Force un tick en mockant wait_for → TimeoutError + stop après."""
    settings = _settings()
    tg = AsyncMock()
    tg.enabled = True
    tg.send = AsyncMock(return_value=True)
    scheduler = DailySummaryScheduler(
        session_factory,
        tg,
        AlertRenderer(),
        settings,
        _FakeDispatcher(),  # type: ignore[arg-type]
    )
    # Appel direct de _send_summary pour couvrir la branche rendu + envoi.
    await scheduler._send_summary()  # noqa: SLF001
    tg.send.assert_awaited_once()
    body = tg.send.await_args.args[0]
    assert "polycopy" in body


@pytest.mark.asyncio
async def test_daily_send_summary_propagates_failure_as_log(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = _settings()
    tg = AsyncMock()
    tg.enabled = True
    tg.send = AsyncMock(return_value=False)  # failure
    scheduler = DailySummaryScheduler(
        session_factory,
        tg,
        AlertRenderer(),
        settings,
        _FakeDispatcher(),  # type: ignore[arg-type]
    )
    await scheduler._send_summary()  # noqa: SLF001 — pas de raise, juste un log warning.


@pytest.mark.asyncio
async def test_daily_run_until_stop(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Force run à aller jusqu'à un tick complet via fake_wait_for."""
    settings = _settings()
    tg = AsyncMock()
    tg.enabled = True
    tg.send = AsyncMock(return_value=True)
    scheduler = DailySummaryScheduler(
        session_factory,
        tg,
        AlertRenderer(),
        settings,
        _FakeDispatcher(),  # type: ignore[arg-type]
    )
    # Patch wait_for pour retourner TimeoutError tout de suite
    original_wait_for = asyncio.wait_for
    call_count = {"n": 0}

    async def fake_wait_for(awaitable: Any, timeout: float) -> Any:  # noqa: ASYNC109
        call_count["n"] += 1
        # Premier appel → TimeoutError (simule le tick), deuxième → stop_event set
        if call_count["n"] == 1:
            return await original_wait_for(awaitable, 0.01)
        # Ensuite stop_event
        return None

    monkeypatch.setattr(
        "polycopy.monitoring.daily_summary_scheduler.asyncio.wait_for",
        fake_wait_for,
    )
    stop = asyncio.Event()

    async def stopper() -> None:
        await asyncio.sleep(0.1)
        stop.set()

    await asyncio.gather(scheduler.run(stop), stopper())
    assert tg.send.await_count >= 1
