"""Tests des filtres Jinja cosmétiques M6."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from polycopy.dashboard import jinja_filters as f


class TestFormatUsd:
    def test_none(self) -> None:
        assert f.format_usd(None) == "—"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.0, "$0.00"),
            (0.45, "$0.45"),
            (12.34, "$12.34"),
            (999.99, "$999.99"),  # juste sous le bord 1000
            (1000.0, "$1.0k"),
            (1234.56, "$1.2k"),
            (2_500_000.0, "$2.5M"),
            (-1234.56, "-$1.2k"),
        ],
    )
    def test_values(self, value: float, expected: str) -> None:
        assert f.format_usd(value) == expected


class TestFormatSize:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, "—"),
            (0.0, "0.00"),
            (3.5, "3.50"),
            (12.345, "12.35"),  # arrondi banker → 12.35 acceptable
        ],
    )
    def test_values(self, value: float | None, expected: str) -> None:
        assert f.format_size(value) == expected


class TestFormatPct:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, "—"),
            (3.92, "+3.9%"),
            (-1.2, "-1.2%"),
            (0.0, "0.0%"),
        ],
    )
    def test_values(self, value: float | None, expected: str) -> None:
        assert f.format_pct(value) == expected

    def test_with_sign_false(self) -> None:
        assert f.format_pct(3.92, with_sign=False) == "3.9%"


class TestHumanizeDt:
    def test_none(self) -> None:
        assert f.humanize_dt(None) == "—"

    def test_seconds(self) -> None:
        now = datetime.now(tz=UTC)
        assert "s" in f.humanize_dt(now - timedelta(seconds=30))

    def test_minutes(self) -> None:
        now = datetime.now(tz=UTC)
        assert f.humanize_dt(now - timedelta(minutes=5)).endswith("min")

    def test_hours(self) -> None:
        now = datetime.now(tz=UTC)
        assert f.humanize_dt(now - timedelta(hours=3)).endswith("h")

    def test_days(self) -> None:
        now = datetime.now(tz=UTC)
        assert f.humanize_dt(now - timedelta(days=3)).endswith("j")

    def test_iso_after_30d(self) -> None:
        old = datetime(2026, 1, 1, tzinfo=UTC)
        out = f.humanize_dt(old)
        assert out == "2026-01-01"

    def test_naive_datetime_is_assumed_utc(self) -> None:
        naive = datetime.utcnow() - timedelta(seconds=10)  # noqa: DTZ003 - intent
        out = f.humanize_dt(naive)
        assert "s" in out

    def test_future_dt(self) -> None:
        future = datetime.now(tz=UTC) + timedelta(seconds=30)
        assert f.humanize_dt(future) == "à l'instant"


class TestShortHash:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, "—"),
            ("", "—"),
            ("0xabcdef1234567890", "0xabcd…7890"),
            ("abcdef1234567890", "abcd…7890"),
        ],
    )
    def test_values(self, value: str | None, expected: str) -> None:
        assert f.short_hash(value) == expected

    def test_short_value_returned_as_is(self) -> None:
        assert f.short_hash("0xabc") == "0xabc"

    def test_custom_width(self) -> None:
        assert f.short_hash("0xabcdef1234567890", width=2) == "0xab…90"


class TestWalletLabel:
    def test_with_label(self) -> None:
        trader = SimpleNamespace(label="Whale", wallet_address="0xabcdef1234567890")
        assert f.wallet_label(trader) == "Whale"

    def test_without_label(self) -> None:
        trader = SimpleNamespace(label=None, wallet_address="0xabcdef1234567890")
        assert f.wallet_label(trader) == "0xabcd…7890"

    def test_dict_input(self) -> None:
        trader = {"label": None, "wallet_address": "0xabcdef1234567890"}
        assert f.wallet_label(trader) == "0xabcd…7890"


class TestScoreToDasharray:
    def test_none(self) -> None:
        assert f.score_to_dasharray(None) == "0 339.292"

    def test_zero(self) -> None:
        assert f.score_to_dasharray(0.0) == "0 339.292"

    def test_full(self) -> None:
        out = f.score_to_dasharray(1.0)
        # Premier nombre proche de la circonférence, second ~0.
        first, _ = out.split(" ")
        assert float(first) == pytest.approx(339.292, rel=1e-3)

    def test_half(self) -> None:
        out = f.score_to_dasharray(0.5)
        first, second = out.split(" ")
        assert float(first) == pytest.approx(169.646, rel=1e-3)
        assert float(second) == pytest.approx(169.646, rel=1e-3)

    def test_clamped_above_one(self) -> None:
        out = f.score_to_dasharray(1.5)
        first, _ = out.split(" ")
        assert float(first) == pytest.approx(339.292, rel=1e-3)

    def test_custom_circumference(self) -> None:
        out = f.score_to_dasharray(0.5, circumference=200.0)
        assert out == "100.000 100.000"


class TestSideIcon:
    def test_buy(self) -> None:
        assert f.side_icon("BUY") == "arrow-up-circle"

    def test_sell(self) -> None:
        assert f.side_icon("SELL") == "arrow-down-circle"

    def test_none_defaults_to_buy(self) -> None:
        assert f.side_icon(None) == "arrow-up-circle"


class TestStatusBadgeClass:
    @pytest.mark.parametrize(
        ("status", "expected_class"),
        [
            ("FILLED", "badge badge-ok"),
            ("APPROVED", "badge badge-ok"),
            ("active", "badge badge-ok"),
            ("REJECTED", "badge badge-error"),
            ("FAILED", "badge badge-error"),
            ("SIMULATED", "badge badge-info"),
            ("shadow", "badge badge-info"),
            ("paused", "badge badge-warning"),
            ("PARTIALLY_FILLED", "badge badge-warning"),
            ("SENT", "badge badge-warning"),
            ("pinned", "badge badge-pinned"),
            ("UNKNOWN", "badge badge-neutral"),
            (None, "badge badge-neutral"),
            ("", "badge badge-neutral"),
        ],
    )
    def test_classes(self, status: str | None, expected_class: str) -> None:
        assert f.status_badge_class(status) == expected_class


class TestSparklineSvg:
    def test_empty(self) -> None:
        out = f.sparkline_svg(None)
        assert "sparkline-empty" in out
        assert "<svg" in out and "</svg>" in out

    def test_single_point_renders_empty(self) -> None:
        out = f.sparkline_svg([(datetime.now(tz=UTC), 1.0)])
        assert "sparkline-empty" in out

    def test_two_points(self) -> None:
        now = datetime.now(tz=UTC)
        out = f.sparkline_svg([(now, 1.0), (now + timedelta(minutes=1), 2.0)])
        assert "<polyline" in out
        assert 'class="sparkline"' in out

    def test_flat_line_doesnt_crash(self) -> None:
        now = datetime.now(tz=UTC)
        out = f.sparkline_svg(
            [(now, 1.0), (now + timedelta(minutes=1), 1.0), (now + timedelta(minutes=2), 1.0)]
        )
        assert "<polyline" in out


class TestAllFilters:
    def test_returns_full_dict(self) -> None:
        d = f.all_filters()
        for name in (
            "format_usd",
            "format_size",
            "format_pct",
            "humanize_dt",
            "short_hash",
            "wallet_label",
            "score_to_dasharray",
            "side_icon",
            "status_badge_class",
            "sparkline_svg",
        ):
            assert name in d
            assert callable(d[name])
