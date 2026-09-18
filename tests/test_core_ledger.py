"""``core/ledger.py`` 单测。"""

from __future__ import annotations

import pytest

from do_modle.core.ledger import Ledger
from do_modle.rules.account import Account
from do_modle.rules.cost import AShareCostModel
from do_modle.rules.portfolio import Portfolio
from tests._helpers import D0, D1, D2, SYMBOL

COST = AShareCostModel()


@pytest.fixture
def ledger() -> Ledger:
    return Ledger(Portfolio([SYMBOL]), Account(initial_cash=100_000.0))


def _buy_trade(qty: int, price: float, d=D0):
    detail = COST.buy(qty, price)
    return COST.to_trade(detail, symbol=SYMBOL, dt=_dt(d))


def _sell_trade(qty: int, price: float, d=D1):
    detail = COST.sell(qty, price)
    return COST.to_trade(detail, symbol=SYMBOL, dt=_dt(d))


def _dt(d):
    from datetime import datetime

    return datetime(d.year, d.month, d.day, 9, 30)


# ==================== 日初 ====================

def test_on_before_trading_rolls_positions(ledger: Ledger) -> None:
    ledger.on_before_trading(D0)
    ledger.on_trade(_buy_trade(1000, 20.0))
    assert ledger.sellable(SYMBOL) == 0
    ledger.on_before_trading(D1)
    assert ledger.sellable(SYMBOL) == 1000


# ==================== 成交 ====================

def test_buy_increases_position_and_reduces_cash(ledger: Ledger) -> None:
    ledger.on_before_trading(D0)
    realized = ledger.on_trade(_buy_trade(1000, 20.0))
    assert ledger.position(SYMBOL) == 1000
    assert ledger.cash == pytest.approx(100_000.0 - 20_012.2011)
    assert realized == 0.0  # 买入不产生已实现盈亏


def test_buy_uses_exec_price_as_cost_basis(ledger: Ledger) -> None:
    """批次成本用**含滑点成交价**——那才是真实付出的价格。"""
    ledger.on_before_trading(D0)
    ledger.on_trade(_buy_trade(1000, 20.0))
    assert ledger.portfolio.book(SYMBOL).avg_cost == pytest.approx(20.01)


def test_sell_reduces_position_and_increases_cash(ledger: Ledger) -> None:
    ledger.on_before_trading(D0)
    ledger.on_trade(_buy_trade(1000, 20.0))
    cash_after_buy = ledger.cash
    ledger.on_before_trading(D1)
    ledger.on_trade(_sell_trade(1000, 22.0))
    assert ledger.position(SYMBOL) == 0
    assert ledger.cash > cash_after_buy


def test_sell_computes_realized_pnl(ledger: Ledger) -> None:
    """已实现盈亏 = Σ (卖出价 − 批次成本) × 数量。"""
    ledger.on_before_trading(D0)
    ledger.on_trade(_buy_trade(1000, 20.0))  # 成本 20.01
    ledger.on_before_trading(D1)
    realized = ledger.on_trade(_sell_trade(1000, 22.0))  # 成交 21.99
    assert realized == pytest.approx((21.99 - 20.01) * 1000)
    assert ledger.realized_pnl == pytest.approx(realized)


def test_realized_pnl_accumulates(ledger: Ledger) -> None:
    ledger.on_before_trading(D0)
    ledger.on_trade(_buy_trade(1000, 20.0))
    ledger.on_before_trading(D1)
    first = ledger.on_trade(_sell_trade(400, 22.0))
    second = ledger.on_trade(_sell_trade(600, 23.0))
    assert ledger.realized_pnl == pytest.approx(first + second)


def test_partial_sell_keeps_position(ledger: Ledger) -> None:
    ledger.on_before_trading(D0)
    ledger.on_trade(_buy_trade(1000, 20.0))
    ledger.on_before_trading(D1)
    ledger.on_trade(_sell_trade(300, 21.0))
    assert ledger.position(SYMBOL) == 700


def test_trade_count(ledger: Ledger) -> None:
    ledger.on_before_trading(D0)
    ledger.on_trade(_buy_trade(1000, 20.0))
    ledger.on_before_trading(D1)
    ledger.on_trade(_sell_trade(1000, 21.0))
    assert ledger.trade_count == 2


def test_on_trades_batch(ledger: Ledger) -> None:
    ledger.on_before_trading(D0)
    total = ledger.on_trades([_buy_trade(1000, 20.0), _buy_trade(500, 20.5)])
    assert total == 0.0
    assert ledger.position(SYMBOL) == 1500


# ==================== 结算 ====================

def test_settle_snapshot(ledger: Ledger) -> None:
    ledger.on_before_trading(D0)
    ledger.on_trade(_buy_trade(1000, 20.0))
    snap = ledger.settle(D0, {SYMBOL: 21.0})
    assert snap.market_value == pytest.approx(21_000.0)
    assert snap.total == pytest.approx(ledger.cash + 21_000.0)


def test_settle_with_prev_close_uses_daily_semantics(ledger: Ledger) -> None:
    ledger.on_before_trading(D0)
    ledger.on_trade(_buy_trade(1000, 20.0))
    ledger.on_before_trading(D1)
    snap = ledger.settle(D1, {SYMBOL: 22.0}, {SYMBOL: 21.0})
    assert snap.position_pnl == pytest.approx(1000.0)


def test_settle_flat_account(ledger: Ledger) -> None:
    ledger.on_before_trading(D0)
    snap = ledger.settle(D0, {})
    assert snap.total == pytest.approx(100_000.0)
    assert snap.market_value == 0.0


# ==================== 查询 ====================

def test_available_cash_accessor(ledger: Ledger) -> None:
    assert ledger.available_cash == pytest.approx(100_000.0)
    ledger.account.freeze(10_000)
    assert ledger.available_cash == pytest.approx(90_000.0)


def test_position_of_unknown_symbol_is_zero(ledger: Ledger) -> None:
    assert ledger.position("sh.600026") == 0
