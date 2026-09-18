"""``core/matching.py`` 单测 —— 撮合规则逐条覆盖。"""

from __future__ import annotations

from datetime import date

import pytest

from do_modle.core.matching import MatchingEngine
from do_modle.objects import RejectReason, Side
from do_modle.rules.cost import AShareCostModel, CostConfig
from do_modle.rules.position import PositionBook
from do_modle.rules.tradability import TradabilityGate
from tests._helpers import D0, D1, D2, MAIN_INSTRUMENT, SYMBOL, make_bar

ENGINE = MatchingEngine()


def _book(*, held: int = 0, open_date: date = D0) -> PositionBook:
    """构造持仓簿：``held`` 股昨仓（可卖）。"""
    book = PositionBook()
    book.before_trading(open_date)
    if held:
        book.buy(open_date, held, 20.0)
        book.before_trading(D1)
    return book


def _order(side: Side, qty: int, *, seq: int = 0):
    from do_modle.objects import OrderRequest

    return OrderRequest(symbol=SYMBOL, dt=None, side=side, quantity=qty, seq=seq)  # type: ignore[arg-type]


BAR = make_bar(d=D1, open_=20.0, prev_close=20.0)


# ==================== 构造期守卫 ====================

def test_execution_price_must_be_next_open() -> None:
    """成交价基准写死 next_open——用当日收盘会引入未来函数。"""
    with pytest.raises(ValueError, match="next_open"):
        MatchingEngine(execution_price="same_close")


def test_unknown_cash_policy_rejected() -> None:
    with pytest.raises(ValueError, match="insufficient_cash_policy"):
        MatchingEngine(insufficient_cash_policy="magic")


def test_future_function_guard() -> None:
    """成交 Bar 日期必须晚于信号日。"""
    with pytest.raises(ValueError, match="未来函数"):
        ENGINE.match(
            [_order(Side.BUY, 100)],
            BAR,
            instrument=MAIN_INSTRUMENT,
            book=_book(),
            available_cash=1e9,
            signal_date=D1,  # 与 bar 同日
        )


def test_signal_date_earlier_is_allowed() -> None:
    result = ENGINE.match(
        [_order(Side.BUY, 100)],
        BAR,
        instrument=MAIN_INSTRUMENT,
        book=_book(),
        available_cash=1e9,
        signal_date=D0,
    )
    assert len(result.trades) == 1


# ==================== 成交价 ====================

def test_buy_fills_at_open_with_slippage() -> None:
    result = ENGINE.match(
        [_order(Side.BUY, 1000)],
        BAR,
        instrument=MAIN_INSTRUMENT,
        book=_book(),
        available_cash=1e9,
    )
    trade = result.trades[0]
    assert trade.raw_price == pytest.approx(20.0)  # 开盘价
    assert trade.price == pytest.approx(20.01)  # 买入上滑
    assert trade.dt == BAR.dt


def test_sell_fills_at_open_with_slippage() -> None:
    result = ENGINE.match(
        [_order(Side.SELL, 1000)],
        BAR,
        instrument=MAIN_INSTRUMENT,
        book=_book(held=1000),
        available_cash=0.0,
    )
    trade = result.trades[0]
    assert trade.price == pytest.approx(19.99)  # 卖出下滑
    assert trade.stamp_tax > 0


# ==================== 规则拒单 ====================

def test_buy_rejected_at_limit_up() -> None:
    bar = make_bar(d=D1, open_=22.0, prev_close=20.0)  # 22.0 = 涨停
    result = ENGINE.match(
        [_order(Side.BUY, 100)],
        bar,
        instrument=MAIN_INSTRUMENT,
        book=_book(),
        available_cash=1e9,
    )
    assert not result.trades
    assert result.rejects[0].reason == RejectReason.LIMIT_UP.value


def test_sell_rejected_at_limit_down() -> None:
    bar = make_bar(d=D1, open_=18.0, prev_close=20.0)
    result = ENGINE.match(
        [_order(Side.SELL, 100)],
        bar,
        instrument=MAIN_INSTRUMENT,
        book=_book(held=1000),
        available_cash=0.0,
    )
    assert not result.trades
    assert result.rejects[0].reason == RejectReason.LIMIT_DOWN.value


def test_sell_rejected_when_nothing_sellable() -> None:
    """T+1：无昨仓时卖单被拒。"""
    result = ENGINE.match(
        [_order(Side.SELL, 100)],
        BAR,
        instrument=MAIN_INSTRUMENT,
        book=_book(),
        available_cash=0.0,
    )
    assert result.rejects[0].reason == RejectReason.T1_VIOLATION.value


def test_suspended_bar_rejects() -> None:
    bar = make_bar(d=D1, suspended=True)
    result = ENGINE.match(
        [_order(Side.BUY, 100)],
        bar,
        instrument=MAIN_INSTRUMENT,
        book=_book(),
        available_cash=1e9,
    )
    assert result.rejects[0].reason == RejectReason.SUSPENDED.value


def test_order_below_one_lot_rejected() -> None:
    result = ENGINE.match(
        [_order(Side.BUY, 50)],
        BAR,
        instrument=MAIN_INSTRUMENT,
        book=_book(),
        available_cash=1e9,
    )
    assert result.rejects[0].reason == RejectReason.LOT_SIZE.value


def test_reject_carries_original_order() -> None:
    order = _order(Side.BUY, 50)
    result = ENGINE.match(
        [order], BAR, instrument=MAIN_INSTRUMENT, book=_book(), available_cash=1e9
    )
    assert result.rejects[0].order is order
    assert result.rejects[0].side is Side.BUY


# ==================== 资金约束 ====================

def test_insufficient_cash_shrinks_to_lot_multiple() -> None:
    result = ENGINE.match(
        [_order(Side.BUY, 1000)],
        BAR,
        instrument=MAIN_INSTRUMENT,
        book=_book(),
        available_cash=5000.0,
    )
    trade = result.trades[0]
    assert trade.quantity == 200
    assert trade.quantity % 100 == 0
    assert -trade.cash_flow <= 5000.0


def test_insufficient_cash_reject_policy() -> None:
    engine = MatchingEngine(insufficient_cash_policy="reject")
    result = engine.match(
        [_order(Side.BUY, 1000)],
        BAR,
        instrument=MAIN_INSTRUMENT,
        book=_book(),
        available_cash=5000.0,
    )
    assert not result.trades
    assert result.rejects[0].reason == RejectReason.INSUFFICIENT_CASH.value


def test_zero_cash_rejects_buy() -> None:
    result = ENGINE.match(
        [_order(Side.BUY, 100)],
        BAR,
        instrument=MAIN_INSTRUMENT,
        book=_book(),
        available_cash=0.0,
    )
    assert result.rejects[0].reason == RejectReason.INSUFFICIENT_CASH.value


def test_multiple_buys_consume_cash_sequentially() -> None:
    result = ENGINE.match(
        [_order(Side.BUY, 1000, seq=0), _order(Side.BUY, 1000, seq=1)],
        BAR,
        instrument=MAIN_INSTRUMENT,
        book=_book(),
        available_cash=30_000.0,
    )
    assert [t.quantity for t in result.trades] == [1000, 400]


def test_sell_with_proceeds_below_fees_rejected() -> None:
    """卖出所得连费用都盖不住 → 拒单，防现金转负。

    用 0.50 元的低价 + 最低佣金 5 元构造。显式指定 ``high``/``low``，避免
    依赖构造器的振幅推算（低价位下 ±1% 会被 tick 取整吃掉，变成一字板）。
    """
    engine = MatchingEngine(
        AShareCostModel(CostConfig(min_commission=5.0)), TradabilityGate()
    )
    bar = make_bar(d=D1, open_=0.50, high=0.51, low=0.49, prev_close=0.50)
    result = engine.match(
        [_order(Side.SELL, 1)],
        bar,
        instrument=MAIN_INSTRUMENT,
        book=_book(held=1),
        available_cash=0.0,
    )
    assert not result.trades
    assert result.rejects[0].reason == RejectReason.INSUFFICIENT_CASH.value


# ==================== 流动性约束 ====================

def test_liquidity_clips_order() -> None:
    bar = make_bar(d=D1, open_=20.0, prev_close=20.0, volume=10_000)
    result = ENGINE.match(
        [_order(Side.BUY, 5000)],
        bar,
        instrument=MAIN_INSTRUMENT,
        book=_book(),
        available_cash=1e9,
    )
    assert result.trades[0].quantity == 1000


# ==================== 订单顺序 ====================

def test_sells_processed_before_buys() -> None:
    """先卖后买：A股 T 日卖出资金 T 日即可用于买入。"""
    result = ENGINE.match(
        [_order(Side.BUY, 1000, seq=0), _order(Side.SELL, 1000, seq=1)],
        BAR,
        instrument=MAIN_INSTRUMENT,
        book=_book(held=1000),
        available_cash=0.0,
    )
    assert [t.side for t in result.trades] == [Side.SELL, Side.BUY]


def test_sell_proceeds_fund_buy() -> None:
    """可用资金为 0 时，只有先卖后买才能成交。"""
    result = ENGINE.match(
        [_order(Side.BUY, 1000, seq=0), _order(Side.SELL, 1000, seq=1)],
        BAR,
        instrument=MAIN_INSTRUMENT,
        book=_book(held=1000),
        available_cash=0.0,
    )
    sell, buy = result.trades
    assert sell.quantity == 1000
    assert buy.quantity == 900  # 卖款 19969.81 < 买 1000 股所需 20020.21


def test_buy_without_sell_proceeds_is_rejected() -> None:
    result = ENGINE.match(
        [_order(Side.BUY, 1000, seq=0)],
        BAR,
        instrument=MAIN_INSTRUMENT,
        book=_book(held=1000),
        available_cash=0.0,
    )
    assert not result.trades
    assert result.rejects[0].reason == RejectReason.INSUFFICIENT_CASH.value


# ==================== 无副作用 ====================

def test_match_does_not_mutate_book() -> None:
    """撮合不碰持仓簿——账务由 Ledger 负责。"""
    book = _book(held=1000)
    before_total, before_sellable = book.total, book.sellable
    ENGINE.match(
        [_order(Side.SELL, 500)],
        BAR,
        instrument=MAIN_INSTRUMENT,
        book=book,
        available_cash=0.0,
    )
    assert book.total == before_total
    assert book.sellable == before_sellable


def test_match_empty_orders() -> None:
    result = ENGINE.match(
        [], BAR, instrument=MAIN_INSTRUMENT, book=_book(), available_cash=1e9
    )
    assert not result.trades and not result.rejects
    assert not bool(result)


# ==================== MatchResult 辅助 ====================

def test_match_result_helpers() -> None:
    result = ENGINE.match(
        [_order(Side.BUY, 1000), _order(Side.BUY, 50)],
        BAR,
        instrument=MAIN_INSTRUMENT,
        book=_book(),
        available_cash=1e9,
    )
    assert result.filled_quantity == 1000
    assert result.cash_flow < 0
    assert result.total_fee > 0
    assert len(result) == 1
    assert bool(result)
