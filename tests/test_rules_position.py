"""``rules/position.py`` 单测 —— T+1 / FIFO / 批次 / 公司行为 / 盈亏双口径。

语义已对 rqalpha ``rqalpha/portfolio/position.py`` 源码核验。
"""

from __future__ import annotations

import pytest

from do_modle.rules.position import PositionBook, PositionLot, T1Violation
from tests._helpers import D0, D1, D2


@pytest.fixture
def book() -> PositionBook:
    b = PositionBook()
    b.before_trading(D0)
    return b


# ---- 1. 会话前置条件 ----

def test_requires_before_trading_first() -> None:
    """未调用 before_trading 就下单 → 明确报错，而非静默算错。"""
    b = PositionBook()
    with pytest.raises(RuntimeError, match="before_trading"):
        b.buy(D0, 100, 10.0)


# ---- 2. T+1 核心 ----

def test_same_day_buy_then_sell_is_rejected(book: PositionBook) -> None:
    """T+1：当日买入当日不可卖。"""
    book.buy(D0, 1000, 10.0)
    assert book.sellable == 0
    with pytest.raises(T1Violation):
        book.sell(D0, 100, 10.5)


def test_sellable_after_rollover(book: PositionBook) -> None:
    book.buy(D0, 1000, 10.0)
    book.before_trading(D1)
    assert book.today_bought == 0
    assert book.sellable == 1000


def test_oversell_rejected(book: PositionBook) -> None:
    book.buy(D0, 1000, 10.0)
    book.before_trading(D1)
    with pytest.raises(T1Violation, match="可卖 1000"):
        book.sell(D1, 1200, 11.0)


def test_only_yesterday_position_is_sellable(book: PositionBook) -> None:
    """T+1 日既卖昨仓又买今仓：可卖量仍只是昨仓。"""
    book.buy(D0, 500, 10.0)
    book.before_trading(D1)
    book.buy(D1, 500, 12.0)
    assert book.total == 1000
    assert book.today_bought == 500
    assert book.sellable == 500


# ---- 3. FIFO ----

def test_fifo_consumes_oldest_lot_first(book: PositionBook) -> None:
    book.buy(D0, 500, 10.0)
    book.before_trading(D1)
    book.buy(D1, 500, 12.0)
    book.before_trading(D2)
    details = book.sell(D2, 700, 15.0)
    assert details == [(D0, 500, 10.0), (D1, 200, 12.0)]
    assert book.total == 300


def test_fifo_partial_close_leaves_correct_lot(book: PositionBook) -> None:
    book.buy(D0, 1000, 10.0)
    book.before_trading(D1)
    book.sell(D1, 400, 11.0)
    assert book.total == 600
    assert len(book.lots()) == 1
    assert book.lots()[0].open_date == D0
    assert book.lots()[0].quantity == 600


def test_full_close_empties_book(book: PositionBook) -> None:
    book.buy(D0, 1000, 10.0)
    book.before_trading(D1)
    book.sell(D1, 1000, 11.0)
    assert book.total == 0
    assert book.lots() == ()
    assert book.avg_cost == 0.0


# ---- 4. 同日加仓 ----

def test_same_day_add_position_merges_and_averages_cost(book: PositionBook) -> None:
    book.buy(D0, 500, 10.0)
    book.buy(D0, 500, 12.0)
    assert len(book.lots()) == 1
    assert book.total == 1000
    assert book.avg_cost == pytest.approx(11.0)


def test_different_day_add_creates_new_lot(book: PositionBook) -> None:
    book.buy(D0, 500, 10.0)
    book.before_trading(D1)
    book.buy(D1, 500, 12.0)
    assert len(book.lots()) == 2
    assert book.avg_cost == pytest.approx(11.0)


# ---- 5. 冻结 ----

def test_freeze_reduces_sellable(book: PositionBook) -> None:
    book.buy(D0, 1000, 10.0)
    book.before_trading(D1)
    book.freeze(400)
    assert book.frozen == 400
    assert book.sellable == 600


def test_freeze_more_than_position_rejected(book: PositionBook) -> None:
    book.buy(D0, 1000, 10.0)
    with pytest.raises(ValueError, match="冻结超过持仓"):
        book.freeze(1001)


def test_unfreeze_restores_sellable(book: PositionBook) -> None:
    book.buy(D0, 1000, 10.0)
    book.before_trading(D1)
    book.freeze(400)
    book.unfreeze(400)
    assert book.frozen == 0
    assert book.sellable == 1000


def test_before_trading_clears_frozen(book: PositionBook) -> None:
    book.buy(D0, 1000, 10.0)
    book.freeze(300)
    book.before_trading(D1)
    assert book.frozen == 0


# ---- 6. 公司行为 ----

def test_share_ratio_scales_quantity_and_dilutes_cost(book: PositionBook) -> None:
    book.buy(D0, 1000, 10.0)
    total_cost_before = book.total_cost
    book.apply_share_ratio(0.3)  # 10 送 3
    assert book.total == 1300
    assert book.avg_cost == pytest.approx(10.0 / 1.3)
    assert book.total_cost == pytest.approx(total_cost_before)


def _three_lot_book() -> PositionBook:
    """构造 3 个批次（每日 100 股），用于测试送转取整误差。"""
    b = PositionBook()
    b.before_trading(D0)
    b.buy(D0, 100, 10.0)
    b.before_trading(D1)
    b.buy(D1, 100, 10.0)
    b.before_trading(D2)
    b.buy(D2, 100, 10.0)
    return b


def test_share_ratio_corrects_rounding_with_expected_quantity() -> None:
    """逐批次取整会累积误差，必须用交易所给出的总股数校正（同 rqalpha）。"""
    uncorrected = _three_lot_book()
    assert len(uncorrected.lots()) == 3
    uncorrected.apply_share_ratio(1 / 3)
    assert uncorrected.total == 399  # 每批 round(133.33)=133 -> 399

    corrected = _three_lot_book()
    corrected.apply_share_ratio(1 / 3, expected_quantity=400)
    assert corrected.total == 400  # 差额 +1 补到最后一个批次
    assert corrected.lots()[-1].quantity == 134


def test_cash_dividend_amount(book: PositionBook) -> None:
    book.buy(D0, 1000, 10.0)
    assert book.receive_cash_dividend(0.5) == pytest.approx(500.0)


def test_negative_dividend_rejected(book: PositionBook) -> None:
    with pytest.raises(ValueError):
        book.receive_cash_dividend(-0.1)


def test_invalid_share_ratio_rejected(book: PositionBook) -> None:
    with pytest.raises(ValueError):
        book.apply_share_ratio(-1.5)


# ---- 7. 盈亏双口径（对 rqalpha 源码核验） ----

def test_cumulative_pnl_is_relative_to_cost(book: PositionBook) -> None:
    """累积口径：今仓与昨仓都相对**成本价**。"""
    book.buy(D0, 1000, 10.0)
    book.before_trading(D1)
    book.buy(D1, 500, 12.0)
    trading, position = book.unrealized_pnl(13.0)
    assert trading == pytest.approx(500 * (13.0 - 12.0))
    assert position == pytest.approx(1000 * (13.0 - 10.0))


def test_daily_pnl_uses_prev_close_not_cost(book: PositionBook) -> None:
    """当日口径：昨仓部分相对**昨收**，不是成本价——这是 rqalpha 的语义。"""
    book.buy(D0, 1000, 10.0)
    book.before_trading(D1)
    trading, position = book.daily_pnl_breakdown(12.0, prev_close=11.0)
    assert trading == pytest.approx(0.0)  # 当日无成交
    assert position == pytest.approx(1000 * (12.0 - 11.0))  # 相对昨收，非成本


def test_daily_pnl_splits_today_trade(book: PositionBook) -> None:
    book.buy(D0, 1000, 10.0)
    book.before_trading(D1)
    book.buy(D1, 500, 12.0)
    trading, position = book.daily_pnl_breakdown(13.0, prev_close=11.0)
    assert trading == pytest.approx(500 * (13.0 - 12.0))
    assert position == pytest.approx(1000 * (13.0 - 11.0))


def test_daily_pnl_zero_when_flat(book: PositionBook) -> None:
    assert book.daily_pnl_breakdown(10.0, prev_close=9.0) == (0.0, 0.0)


# ---- 8. 参数校验 ----

@pytest.mark.parametrize("quantity", [0, -100])
def test_buy_rejects_non_positive_quantity(book: PositionBook, quantity: int) -> None:
    with pytest.raises(ValueError):
        book.buy(D0, quantity, 10.0)


def test_sell_rejects_non_positive_quantity(book: PositionBook) -> None:
    with pytest.raises(ValueError):
        book.sell(D0, 0, 10.0)


def test_lot_rejects_negative_quantity() -> None:
    with pytest.raises(ValueError):
        PositionLot(D0, -1, 10.0)
