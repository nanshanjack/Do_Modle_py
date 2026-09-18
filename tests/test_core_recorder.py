"""``core/recorder.py`` 单测。"""

from __future__ import annotations

import pytest

from do_modle.core.recorder import BASE_FIELDS, Recorder
from tests._helpers import D0, D1, D2


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


def test_snapshot_records_row(recorder: Recorder) -> None:
    row = recorder.snapshot(D0, cash=100.0, position=0)
    assert row == {"date": D0, "cash": 100.0, "position": 0}
    assert len(recorder) == 1


def test_snapshot_rejects_date_in_state(recorder: Recorder) -> None:
    with pytest.raises(ValueError, match="date"):
        recorder.snapshot(D0, date="2026-01-01")


def test_records_are_ordered(recorder: Recorder) -> None:
    for d in (D0, D1, D2):
        recorder.snapshot(d, total=1.0)
    assert recorder.field("date") == [D0, D1, D2]


def test_last(recorder: Recorder) -> None:
    assert recorder.last is None
    recorder.snapshot(D0, total=1.0)
    recorder.snapshot(D1, total=2.0)
    assert recorder.last is not None
    assert recorder.last["total"] == 2.0


def test_field_missing_returns_none(recorder: Recorder) -> None:
    recorder.snapshot(D0, a=1)
    recorder.snapshot(D1, b=2)
    assert recorder.field("a") == [1, None]
    assert recorder.field("nope") == [None, None]


def test_columns_in_first_seen_order(recorder: Recorder) -> None:
    recorder.snapshot(D0, cash=1.0, position=0)
    recorder.snapshot(D1, cash=2.0, extra=9)
    assert recorder.columns() == ["date", "cash", "position", "extra"]


def test_to_rows_without_fields_returns_copies(recorder: Recorder) -> None:
    recorder.snapshot(D0, cash=1.0)
    rows = recorder.to_rows()
    rows[0]["cash"] = 999
    assert recorder.records[0]["cash"] == 1.0


def test_to_rows_with_fixed_fields_pads_missing(recorder: Recorder) -> None:
    recorder.snapshot(D0, cash=1.0)
    rows = recorder.to_rows(("date", "cash", "trades"))
    assert rows == [{"date": D0, "cash": 1.0, "trades": None}]


def test_base_fields_are_stable() -> None:
    assert BASE_FIELDS[0] == "date"
    assert "total" in BASE_FIELDS
    assert "daily_pnl" in BASE_FIELDS


def test_iter(recorder: Recorder) -> None:
    recorder.snapshot(D0, total=1.0)
    assert [r["total"] for r in recorder] == [1.0]


def test_clear(recorder: Recorder) -> None:
    recorder.snapshot(D0, total=1.0)
    recorder.clear()
    assert len(recorder) == 0
    assert recorder.last is None


def test_flush_returns_count(recorder: Recorder) -> None:
    recorder.snapshot(D0, total=1.0)
    recorder.snapshot(D1, total=2.0)
    assert recorder.flush() == 2
