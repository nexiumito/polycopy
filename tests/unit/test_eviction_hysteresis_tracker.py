"""Tests HysteresisTracker M5_bis Phase B.7.

In-memory, stateless hors son propre dict — testable sans fixture.
"""

from __future__ import annotations

from polycopy.discovery.eviction import HysteresisTracker


def test_tick_starts_at_one() -> None:
    tracker = HysteresisTracker()
    n = tracker.tick(
        "0xa",
        direction="eviction",
        target_wallet="0xworst",
        current_delta=0.20,
    )
    assert n == 1
    assert tracker.count("0xa") == 1


def test_tick_increments_on_same_direction_and_target() -> None:
    tracker = HysteresisTracker()
    tracker.tick("0xa", direction="eviction", target_wallet="0xw", current_delta=0.20)
    n = tracker.tick("0xa", direction="eviction", target_wallet="0xw", current_delta=0.22)
    assert n == 2


def test_tick_resets_on_direction_change() -> None:
    tracker = HysteresisTracker()
    tracker.tick("0xa", direction="eviction", target_wallet="0xw", current_delta=0.20)
    tracker.tick("0xa", direction="eviction", target_wallet="0xw", current_delta=0.20)
    # Direction change : reset à 1.
    n = tracker.tick("0xa", direction="abort", target_wallet="0xw", current_delta=0.05)
    assert n == 1


def test_tick_resets_on_target_change() -> None:
    tracker = HysteresisTracker()
    tracker.tick("0xa", direction="eviction", target_wallet="0xw1", current_delta=0.20)
    tracker.tick("0xa", direction="eviction", target_wallet="0xw1", current_delta=0.20)
    n = tracker.tick("0xa", direction="eviction", target_wallet="0xw2", current_delta=0.25)
    assert n == 1


def test_reset_removes_state() -> None:
    tracker = HysteresisTracker()
    tracker.tick("0xa", direction="eviction", target_wallet="0xw", current_delta=0.20)
    tracker.reset("0xa")
    assert tracker.count("0xa") == 0
    assert tracker.get("0xa") is None


def test_reset_unknown_wallet_noop() -> None:
    tracker = HysteresisTracker()
    tracker.reset("0xunknown")  # ne doit pas lever


def test_count_absent_returns_zero() -> None:
    tracker = HysteresisTracker()
    assert tracker.count("0xa") == 0


def test_wallet_address_normalized_lowercase() -> None:
    tracker = HysteresisTracker()
    tracker.tick("0xABC", direction="eviction", target_wallet="0xW", current_delta=0.20)
    assert tracker.count("0xabc") == 1
    assert tracker.count("0xABC") == 1  # lookup tolérant


def test_snapshot_is_defensive_copy() -> None:
    tracker = HysteresisTracker()
    tracker.tick("0xa", direction="eviction", target_wallet="0xw", current_delta=0.20)
    snap = tracker.snapshot()
    assert "0xa" in snap
    # Modifier le snapshot ne doit pas affecter le tracker.
    del snap["0xa"]
    assert tracker.count("0xa") == 1


def test_last_delta_updates_each_tick() -> None:
    tracker = HysteresisTracker()
    tracker.tick("0xa", direction="eviction", target_wallet="0xw", current_delta=0.20)
    tracker.tick("0xa", direction="eviction", target_wallet="0xw", current_delta=0.28)
    state = tracker.get("0xa")
    assert state is not None
    assert state.last_delta == 0.28


def test_metadata_propagated_on_new_hysteresis() -> None:
    tracker = HysteresisTracker()
    tracker.tick(
        "0xa",
        direction="eviction",
        target_wallet="0xw",
        current_delta=0.20,
        metadata={"first_score": "0.91"},
    )
    state = tracker.get("0xa")
    assert state is not None
    assert state.metadata == {"first_score": "0.91"}


def test_len_reflects_tracked_count() -> None:
    tracker = HysteresisTracker()
    assert len(tracker) == 0
    tracker.tick("0xa", direction="eviction", target_wallet="0xw", current_delta=0.20)
    tracker.tick("0xb", direction="abort", target_wallet="0xw", current_delta=0.05)
    assert len(tracker) == 2
    tracker.reset("0xa")
    assert len(tracker) == 1
