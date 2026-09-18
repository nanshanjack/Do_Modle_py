"""测试共享常量与构造器（不依赖 pytest，可被任何测试直接 import）。"""

from __future__ import annotations

from datetime import date, datetime

from do_modle.numeric import round_to_tick
from do_modle.objects import AdjustFlag, Bar, Board, Instrument

D0 = date(2026, 9, 14)
D1 = date(2026, 9, 15)
D2 = date(2026, 9, 16)

SYMBOL = "sh.601872"

MAIN_INSTRUMENT = Instrument(
    symbol=SYMBOL,
    name="招商轮船",
    board=Board.MAIN,
    list_date=date(2006, 12, 1),
)

GEM_INSTRUMENT = Instrument(
    symbol="sz.300750", name="创业板样本", board=Board.GEM, list_date=date(2018, 6, 11)
)

STAR_INSTRUMENT = Instrument(
    symbol="sh.688981", name="科创板样本", board=Board.STAR, list_date=date(2020, 7, 16)
)

BSE_INSTRUMENT = Instrument(
    symbol="bj.832000", name="北交所样本", board=Board.BSE, list_date=date(2021, 11, 15)
)


def make_bar(
    *,
    symbol: str = SYMBOL,
    d: date = D1,
    open_: float = 20.0,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    prev_close: float = 20.0,
    volume: float = 0.0,
    amount: float = 0.0,
    suspended: bool = False,
    is_st: bool = False,
    limit_up: float | None = None,
    limit_down: float | None = None,
    adjust_flag: AdjustFlag = AdjustFlag.NONE,
) -> Bar:
    """构造一根 Bar。

    **默认构造"正常交易日"**：``high``/``low`` 各留 ±1% 振幅，避免误判成一字板。
    需要一字板时显式传 ``high=low``。
    """
    if high is None:
        high = round_to_tick(open_ * 1.01)
    if low is None:
        low = round_to_tick(open_ * 0.99)
    return Bar(
        symbol=symbol,
        dt=datetime(d.year, d.month, d.day, 15, 0),
        open=open_,
        high=high,
        low=low,
        close=open_ if close is None else close,
        prev_close=prev_close,
        volume=volume,
        amount=amount,
        adjust_flag=adjust_flag,
        is_suspended=suspended,
        is_st=is_st,
        limit_up=limit_up,
        limit_down=limit_down,
    )


def make_instrument(
    symbol: str = SYMBOL,
    board: Board = Board.MAIN,
    *,
    name: str = "样本",
    list_date: date = date(2006, 12, 1),
    lot_size: int = 100,
    tick_size: float = 0.01,
) -> Instrument:
    return Instrument(
        symbol=symbol,
        name=name,
        board=board,
        list_date=list_date,
        lot_size=lot_size,
        tick_size=tick_size,
    )
