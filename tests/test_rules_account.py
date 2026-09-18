"""``rules/account.py`` 单测 —— 资金 / 冻结 / 结算快照。"""

from __future__ import annotations

import pytest

from do_modle.objects import Side
from do_modle.rules.account import Account
from do_modle.rules.cost import AShareCostModel
from do_modle.rules.portfolio import Portfolio
from tests._helpers import D0, D1, SYMBOL

MODEL = AShareCostModel()


def _account_with_position(cash: float = 100_000.0):
    """建一个持有 1000 股 @20 元的账户组合。"""
    acc = Account(initial_cash=cash)
    pf = Portfolio([SYMBOL])
    book = pf.book(SYMBOL)
    book.before_trading(D0)
    detail = MODEL.buy(1000, 20.0)
    book.buy(D0, 1000, detail.exec_price)
    acc.on_trade(MODEL.to_trade(detail, symbol=SYMBOL, dt=None))
    return acc, pf, book


# ---- 1. 初始状态 ----

def test_initial_cash_defaults_to_cash() -> None:
    assert Account(initial_cash=100_000).cash == pytest.approx(100_000.0)


def test_negative_initial_cash_rejected() -> None:
    with pytest.raises(ValueError):
        Account(initial_cash=-1)


def test_available_cash_excludes_frozen() -> None:
    acc = Account(initial_cash=100_000)
    acc.freeze(30_000)
    assert acc.available_cash == pytest.approx(70_000.0)


# ---- 2. 冻结 ----

def test_freeze_then_unfreeze() -> None:
    acc = Account(initial_cash=100_000)
    acc.freeze(30_000)
    acc.unfreeze(30_000)
    assert acc.frozen == 0.0
    assert acc.available_cash == pytest.approx(100_000.0)


def test_freeze_more_than_available_rejected() -> None:
    acc = Account(initial_cash=100_000)
    with pytest.raises(ValueError, match="冻结超过可用资金"):
        acc.freeze(100_001)


def test_unfreeze_is_floored_at_zero() -> None:
    acc = Account(initial_cash=100_000)
    acc.unfreeze(999)
    assert acc.frozen == 0.0


# ---- 3. 成交 ----

def test_buy_trade_reduces_cash() -> None:
    acc = Account(initial_cash=100_000)
    detail = MODEL.buy(1000, 20.0)
    acc.on_trade(MODEL.to_trade(detail, symbol=SYMBOL, dt=None))
    assert acc.cash == pytest.approx(100_000.0 - 20_012.2011)


def test_sell_trade_increases_cash() -> None:
    acc = Account(initial_cash=100_000)
    detail = MODEL.sell(1000, 20.0)
    acc.on_trade(MODEL.to_trade(detail, symbol=SYMBOL, dt=None))
    assert acc.cash == pytest.approx(100_000.0 + 19_977.8061)


def test_insufficient_cash_rejected() -> None:
    acc = Account(initial_cash=1_000)
    detail = MODEL.buy(1000, 20.0)
    with pytest.raises(ValueError, match="现金不足"):
        acc.on_trade(MODEL.to_trade(detail, symbol=SYMBOL, dt=None))


# ---- 4. 结算 ----

def test_settle_snapshot_total_equals_cash_plus_market_value() -> None:
    acc, pf, _ = _account_with_position()
    snap = acc.settle(D0, pf, {SYMBOL: 21.0})
    assert snap.market_value == pytest.approx(21_000.0)
    assert snap.total == pytest.approx(acc.cash + 21_000.0)


def test_settle_cumulative_pnl_relative_to_cost() -> None:
    acc, pf, _ = _account_with_position()
    snap = acc.settle(D0, pf, {SYMBOL: 21.0})
    assert snap.cumulative_pnl == pytest.approx(1000 * (21.0 - 20.01))


def test_settle_daily_pnl_uses_prev_close_for_old_position() -> None:
    """昨仓的当日盈亏相对昨收，而非成本价（rqalpha 语义）。"""
    acc, pf, _ = _account_with_position()
    pf.before_trading(D1)
    snap = acc.settle(D1, pf, {SYMBOL: 22.0}, {SYMBOL: 21.0})
    assert snap.trading_pnl == pytest.approx(0.0)
    assert snap.position_pnl == pytest.approx(1000 * (22.0 - 21.0))
    assert snap.daily_pnl == pytest.approx(1000.0)


def test_settle_without_prev_close_falls_back_to_cumulative_split() -> None:
    acc, pf, _ = _account_with_position()
    pf.before_trading(D1)
    snap = acc.settle(D1, pf, {SYMBOL: 22.0})
    assert snap.position_pnl == pytest.approx(1000 * (22.0 - 20.01))


def test_settle_appends_snapshots() -> None:
    acc, pf, _ = _account_with_position()
    acc.settle(D0, pf, {SYMBOL: 21.0})
    acc.settle(D1, pf, {SYMBOL: 21.5})
    assert len(acc.snapshots) == 2
    assert acc.last_snapshot is not None
    assert acc.last_snapshot.dt == D1


def test_flat_account_settles_to_cash() -> None:
    acc = Account(initial_cash=50_000)
    pf = Portfolio([SYMBOL])
    snap = acc.settle(D0, pf, {})
    assert snap.total == pytest.approx(50_000.0)
    assert snap.market_value == 0.0
    assert snap.daily_pnl == 0.0


# ---- 5. 辅助 ----

def test_max_buy_quantity_aligns_to_lot() -> None:
    acc = Account(initial_cash=10_000)
    qty = acc.max_buy_quantity(20.0)
    assert qty == 500  # floor(10000 / (20*100)) * 100 = 5 手


def test_max_buy_quantity_zero_when_price_too_high() -> None:
    acc = Account(initial_cash=100)
    assert acc.max_buy_quantity(20.0) == 0


def test_max_buy_quantity_respects_frozen() -> None:
    acc = Account(initial_cash=10_000)
    acc.freeze(9_000)
    assert acc.max_buy_quantity(20.0) == 0


def test_expected_cash_after_buy_and_sell() -> None:
    acc = Account(initial_cash=10_000)
    assert acc.expected_cash_after(Side.BUY, 1_000, 1.0) == pytest.approx(8_999.0)
    assert acc.expected_cash_after(Side.SELL, 1_000, 1.0) == pytest.approx(10_999.0)
