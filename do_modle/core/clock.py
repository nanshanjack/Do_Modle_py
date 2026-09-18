"""统一时间源。

**策略代码不得直接调用 ``datetime.now()``**——回测用事件时间、实盘用真实时间，
必须经由 ``Clock``。这是回测可复现的前提之一。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Iterator, Literal, Sequence

__all__ = ["Clock", "MARKET_OPEN", "MARKET_CLOSE", "PRE_OPEN", "SETTLE_TIME"]

PRE_OPEN = time(9, 0)
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(15, 0)
SETTLE_TIME = time(15, 5)

ClockMode = Literal["backtest", "live"]


class Clock:
    """交易日历驱动的时钟。

    >>> c = Clock([date(2026, 9, 14), date(2026, 9, 15)])
    >>> c.advance()
    datetime.date(2026, 9, 14)
    >>> c.advance()
    datetime.date(2026, 9, 15)
    >>> c.advance() is None
    True
    """

    def __init__(
        self,
        calendar: Sequence[date],
        mode: ClockMode = "backtest",
        *,
        close_time: time = MARKET_CLOSE,
    ) -> None:
        if mode not in ("backtest", "live"):
            raise ValueError(f"未知模式: {mode!r}")
        self._calendar: tuple[date, ...] = tuple(sorted(set(calendar)))
        self.mode: ClockMode = mode
        self.close_time = close_time
        self._index = -1

    # ---- 迭代 ----

    def __iter__(self) -> Iterator[date]:
        return iter(self._calendar)

    def __len__(self) -> int:
        return len(self._calendar)

    def advance(self) -> date | None:
        """推进一个交易日；越界返回 ``None``（不回退）。"""
        self._index += 1
        return self.current

    @property
    def current(self) -> date | None:
        if 0 <= self._index < len(self._calendar):
            return self._calendar[self._index]
        return None

    @property
    def index(self) -> int:
        return self._index

    @property
    def finished(self) -> bool:
        return self._index >= len(self._calendar)

    def reset(self) -> None:
        self._index = -1

    # ---- 时间 ----

    @property
    def now(self) -> datetime:
        """当前时刻。

        回测模式：当前交易日的 ``close_time``（默认 15:00）。
        实盘模式：真实系统时间。
        """
        if self.mode == "live":
            return datetime.now()
        if self.current is None:
            raise RuntimeError("时钟尚未推进到任何交易日")
        return datetime.combine(self.current, self.close_time)

    def at(self, t: time) -> datetime:
        """当前交易日的指定时刻（回测模式）。"""
        if self.current is None:
            raise RuntimeError("时钟尚未推进到任何交易日")
        return datetime.combine(self.current, t)

    # ---- 查询 ----

    def dates_between(self, start: date, end: date) -> tuple[date, ...]:
        return tuple(d for d in self._calendar if start <= d <= end)

    def next_date(self, d: date) -> date | None:
        """返回 ``d`` 之后的下一个交易日。"""
        for item in self._calendar:
            if item > d:
                return item
        return None

    def previous_date(self, d: date) -> date | None:
        prev: date | None = None
        for item in self._calendar:
            if item >= d:
                break
            prev = item
        return prev

    @property
    def calendar(self) -> tuple[date, ...]:
        return self._calendar
