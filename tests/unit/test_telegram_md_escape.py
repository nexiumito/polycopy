"""Tests du filter ``telegram_md_escape`` et helpers (M7 §9.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from polycopy.monitoring.md_escape import (
    _ESCAPE_CHARS,
    format_usd_tg,
    humanize_dt_tg,
    humanize_duration,
    telegram_md_escape,
    wallet_short,
)

# -- telegram_md_escape ------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hello", "hello"),
        ("127.0.0.1", "127\\.0\\.0\\.1"),
        ("-3.2%", "\\-3\\.2%"),
        ("(voir)", "\\(voir\\)"),
        ("my_wallet", "my\\_wallet"),
        ("[slug]", "\\[slug\\]"),
        ("", ""),
    ],
)
def test_md_escape_known_cases(raw: str, expected: str) -> None:
    assert telegram_md_escape(raw) == expected


def test_md_escape_none_returns_empty() -> None:
    assert telegram_md_escape(None) == ""


def test_md_escape_float_formats_str() -> None:
    assert telegram_md_escape(3.14) == "3\\.14"


def test_md_escape_int_formats_str() -> None:
    assert telegram_md_escape(42) == "42"


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=200))
@example("")
@example("...")
@example("_*[]()~`>#+-=|{}.!")
def test_md_escape_property_no_unescaped_special(raw: str) -> None:
    """Aucun caractère spécial ne doit apparaître non-précédé de ``\\``."""
    out = telegram_md_escape(raw)
    i = 0
    while i < len(out):
        char = out[i]
        if char in _ESCAPE_CHARS:
            # Doit être précédé d'un backslash à l'index i-1.
            assert i > 0 and out[i - 1] == "\\", (
                f"Unescaped {char!r} at index {i} in {out!r} (from {raw!r})"
            )
        i += 1


# -- wallet_short ------------------------------------------------------------


def test_wallet_short_normal() -> None:
    assert wallet_short("0xabcdef1234567890abcdef", 4) == "0xabcd…cdef"


def test_wallet_short_too_short_returns_as_is() -> None:
    assert wallet_short("0xab", 4) == "0xab"


def test_wallet_short_none_returns_empty() -> None:
    assert wallet_short(None) == ""


def test_wallet_short_empty_returns_empty() -> None:
    assert wallet_short("") == ""


# -- format_usd_tg -----------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (1234.56, "$1\\.2k"),
        (0.45, "$0\\.45"),
        (999.99, "$999\\.99"),
        (1000.0, "$1\\.0k"),
        (-1500.0, "$\\-1\\.5k"),
    ],
)
def test_format_usd_tg(amount: float, expected: str) -> None:
    """``$`` n'est pas un escape MarkdownV2 (cf. core.telegram.org/bots/api)."""
    assert format_usd_tg(amount) == expected


def test_format_usd_tg_none() -> None:
    assert format_usd_tg(None) == "—"


# -- humanize_dt_tg ----------------------------------------------------------


def test_humanize_dt_tg_aware_utc() -> None:
    dt = datetime(2026, 4, 18, 14, 30, 0, tzinfo=UTC)
    assert humanize_dt_tg(dt) == "2026\\-04\\-18 14:30 UTC"


def test_humanize_dt_tg_naive_treated_as_utc() -> None:
    dt = datetime(2026, 4, 18, 14, 30, 0)
    assert humanize_dt_tg(dt) == "2026\\-04\\-18 14:30 UTC"


def test_humanize_dt_tg_other_tz_converted() -> None:
    tz = timezone(timedelta(hours=2))
    dt = datetime(2026, 4, 18, 16, 30, 0, tzinfo=tz)
    assert humanize_dt_tg(dt) == "2026\\-04\\-18 14:30 UTC"


def test_humanize_dt_tg_none() -> None:
    assert humanize_dt_tg(None) == "—"


# -- humanize_duration -------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (30, "30 s"),
        (65, "1 min"),
        (300, "5 min"),
        (3600 * 2 + 14 * 60, "2 h 14 min"),
        (86400 * 3 + 3600 * 4, "3 j 4 h"),
        (0, "0 s"),
    ],
)
def test_humanize_duration(seconds: float, expected: str) -> None:
    assert humanize_duration(seconds) == expected
