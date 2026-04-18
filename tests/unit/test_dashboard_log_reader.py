"""Tests `polycopy.dashboard.log_reader` — read_log_tail + filter_entries."""

from __future__ import annotations

import json
from pathlib import Path

from polycopy.dashboard.log_reader import (
    LogEntry,
    filter_entries,
    read_log_tail,
)

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "log_sample.jsonl"


def test_log_entry_parses_fixture_line() -> None:
    raw = _FIXTURE.read_text().splitlines()[0]
    data = json.loads(raw)
    entry = LogEntry.model_validate(data)
    assert entry.event
    assert entry.level


def test_log_entry_extra_fields_preserved() -> None:
    entry = LogEntry.model_validate(
        {
            "event": "trade_detected",
            "level": "info",
            "wallet": "0xabc",
            "size": 12.5,
            "timestamp": "2026-04-18T10:00:00Z",
        }
    )
    fields = entry.all_fields()
    assert fields["wallet"] == "0xabc"
    assert fields["size"] == 12.5
    assert entry.event == "trade_detected"


def test_read_log_tail_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_log_tail(tmp_path / "nope.log", 10) == []


def test_read_log_tail_reads_fixture() -> None:
    entries = read_log_tail(_FIXTURE, 1000)
    assert len(entries) >= 30
    assert all(isinstance(e, LogEntry) for e in entries)


def test_read_log_tail_respects_max_lines(tmp_path: Path) -> None:
    log = tmp_path / "small.log"
    lines = [json.dumps({"event": f"e_{i}", "level": "info"}) for i in range(20)]
    log.write_text("\n".join(lines) + "\n")
    entries = read_log_tail(log, 5)
    assert len(entries) == 5
    # Les 5 derniers events sont e_15..e_19.
    assert entries[-1].event == "e_19"


def test_read_log_tail_skips_malformed_lines(tmp_path: Path) -> None:
    log = tmp_path / "mixed.log"
    log.write_text(
        '{"event":"ok","level":"info"}\n'
        "INFO  [alembic.runtime] Will assume non-transactional DDL.\n"
        "not_json_at_all\n"
        '{"event":"ok2","level":"warning"}\n'
    )
    entries = read_log_tail(log, 100)
    events = [e.event for e in entries]
    assert events == ["ok", "ok2"]


def test_filter_entries_levels_intersection() -> None:
    entries = [
        LogEntry.model_validate({"event": "a", "level": "info"}),
        LogEntry.model_validate({"event": "b", "level": "warning"}),
        LogEntry.model_validate({"event": "c", "level": "error"}),
    ]
    out = filter_entries(entries, levels={"WARNING", "ERROR"})
    assert [e.event for e in out] == ["b", "c"]


def test_filter_entries_event_types() -> None:
    entries = [
        LogEntry.model_validate({"event": "trade_detected", "level": "info"}),
        LogEntry.model_validate({"event": "order_filled", "level": "info"}),
    ]
    out = filter_entries(entries, event_types={"trade_detected"})
    assert [e.event for e in out] == ["trade_detected"]


def test_filter_entries_q_substring_case_insensitive() -> None:
    entries = [
        LogEntry.model_validate({"event": "a", "level": "info", "wallet": "0xABC"}),
        LogEntry.model_validate({"event": "b", "level": "info", "wallet": "0xDEF"}),
    ]
    out = filter_entries(entries, q="0xabc")
    assert [e.event for e in out] == ["a"]


def test_filter_entries_combination() -> None:
    entries = [
        LogEntry.model_validate({"event": "trade_detected", "level": "info", "x": "kw"}),
        LogEntry.model_validate({"event": "trade_detected", "level": "warning", "x": "kw"}),
        LogEntry.model_validate({"event": "trade_detected", "level": "warning", "x": "other"}),
        LogEntry.model_validate({"event": "order_filled", "level": "warning", "x": "kw"}),
    ]
    out = filter_entries(
        entries,
        levels={"WARNING"},
        event_types={"trade_detected"},
        q="kw",
    )
    assert len(out) == 1


def test_filter_entries_empty_filters_returns_all() -> None:
    entries = [LogEntry.model_validate({"event": "a", "level": "info"})]
    assert filter_entries(entries, levels=None, event_types=None, q=None) == entries


def test_read_log_tail_handles_empty_file(tmp_path: Path) -> None:
    log = tmp_path / "empty.log"
    log.write_text("")
    assert read_log_tail(log, 10) == []
