"""测试用的假数据源，不依赖任何网络。"""

from __future__ import annotations

from datetime import date
from typing import Sequence

from do_modle.data.provider import DataProvider, register
from do_modle.objects import AdjustFlag, Bar, Board, CorporateAction, Instrument

__all__ = ["FakeProvider"]


@register("fake")
class FakeProvider(DataProvider):
    """内存数据源：完全可控，用于测注册表与 PIT 流程。"""

    name = "fake"

    def __init__(
        self,
        *,
        bars: dict[tuple[str, AdjustFlag], list[Bar]] | None = None,
        instruments: dict[str, Instrument] | None = None,
        calendar: Sequence[date] | None = None,
        suspended: dict[date, set[str]] | None = None,
        risk_warning: dict[date, set[str]] | None = None,
        dividends: dict[str, list[CorporateAction]] | None = None,
        board_override: dict[str, Board] | None = None,
    ) -> None:
        self._bars = bars or {}
        self._instruments = instruments or {}
        self._calendar = list(calendar or [])
        self._suspended = suspended or {}
        self._risk = risk_warning or {}
        self._dividends = dividends or {}
        self._board = board_override or {}
        self.connected = False
        self.connect_calls = 0

    # ---- DataProvider ----

    def get_bars(
        self,
        symbol: str,
        freq: str = "1d",
        start: date | None = None,
        end: date | None = None,
        adjust: AdjustFlag = AdjustFlag.NONE,
    ) -> list[Bar]:
        bars = list(self._bars.get((symbol, adjust), []))
        if start:
            bars = [b for b in bars if b.dt.date() >= start]
        if end:
            bars = [b for b in bars if b.dt.date() <= end]
        return bars

    def get_instrument(self, symbol: str) -> Instrument:
        if symbol not in self._instruments:
            raise KeyError(f"FakeProvider 未登记标的: {symbol}")
        return self._instruments[symbol]

    def get_calendar(self, start: date, end: date) -> list[date]:
        return [d for d in self._calendar if start <= d <= end]

    def get_board(self, symbol: str, d: date) -> Board:
        return self._board.get(symbol, super().get_board(symbol, d))

    def get_suspended(self, d: date) -> set[str]:
        return set(self._suspended.get(d, set()))

    def get_risk_warning(self, d: date) -> set[str]:
        return set(self._risk.get(d, set()))

    def get_dividends(self, symbol: str, start: date, end: date) -> list[CorporateAction]:
        return [c for c in self._dividends.get(symbol, []) if start <= c.ex_date <= end]

    def connect(self) -> None:
        self.connected = True
        self.connect_calls += 1

    def disconnect(self) -> None:
        self.connected = False
