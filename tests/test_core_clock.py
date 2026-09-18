"""``core/clock.py`` 单测。"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from do_modle.core.clock import MARKET_CLOSE, Clock
from tests._helpers import D0, D1, D2


@pytest.fixture
def clock() -> Clock:
    return Clock([D0, D1, D2])


def test_advance_walks_calendar(clock: Clock) -> None:
    assert clock.advance() == D0
    assert clock.advance() == D1
    assert clock.advance() == D2
    assert clock.advance() is None


def test_advance_does_not_go_past_end(clock: Clock) -> None:
    for _ in range(10):
        clock.advance()
    assert clock.current is None
    assert clock.finished is True


def test_current_before_advance_is_none(clock: Clock) -> None:
    assert clock.current is None


def test_calendar_is_sorted_and_deduped() -> None:
    c = Clock([D2, D0, D1, D0])
    assert c.calendar == (D0, D1, D2)


def test_reset(clock: Clock) -> None:
    clock.advance()
    clock.advance()
    clock.reset()
    assert clock.index == -1
    assert clock.advance() == D0


def test_now_requires_advance(clock: Clock) -> None:
    with pytest.raises(RuntimeError, match="尚未推进"):
        clock.now


def test_now_is_close_time_in_backtest(clock: Clock) -> None:
    clock.advance()
    assert clock.now == datetime.combine(D0, MARKET_CLOSE)


def test_at_returns_specific_time(clock: Clock) -> None:
    clock.advance()
    assert clock.at(time(9, 0)) == datetime(2026, 9, 14, 9, 0)
    assert clock.at(time(15, 5)) == datetime(2026, 9, 14, 15, 5)


def test_live_mode_uses_real_time() -> None:
    c = Clock([D0], mode="live")
    c.advance()
    assert abs((c.now - datetime.now()).total_seconds()) < 5


def test_unknown_mode_rejected() -> None:
    with pytest.raises(ValueError, match="未知模式"):
        Clock([D0], mode="replay")  # type: ignore[arg-type]


def test_next_date(clock: Clock) -> None:
    assert clock.next_date(D0) == D1
    assert clock.next_date(D2) is None


def test_previous_date(clock: Clock) -> None:
    assert clock.previous_date(D2) == D1
    assert clock.previous_date(D0) is None


def test_dates_between(clock: Clock) -> None:
    assert clock.dates_between(D0, D1) == (D0, D1)


def test_iter_and_len(clock: Clock) -> None:
    assert list(clock) == [D0, D1, D2]
    assert len(clock) == 3


def test_iter_does_not_move_index(clock: Clock) -> None:
    list(clock)
    assert clock.index == -1


def test_custom_close_time() -> None:
    c = Clock([D0], close_time=time(14, 57))
    c.advance()
    assert c.now == datetime(2026, 9, 14, 14, 57)


def test_empty_calendar() -> None:
    c = Clock([])
    assert c.advance() is None
    assert len(c) == 0


def test_date_type_preserved() -> None:
    c = Clock([D0])
    assert isinstance(c.advance(), date)
