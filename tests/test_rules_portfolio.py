"""``rules/portfolio.py`` 单测 —— 多标的容器（阶段一只有 1 个 key，但接口必须对）。"""

from __future__ import annotations

import pytest

from do_modle.rules.portfolio import Portfolio
from tests._helpers import D0, D1, SYMBOL

SYM_B = "sh.600026"  # 中远海能（用于验证多标的容器，不进入交易链路）


@pytest.fixture
def portfolio() -> Portfolio:
    return Portfolio([SYMBOL])


def test_book_autocreates(portfolio: Portfolio) -> None:
    book = portfolio.book("sh.601975")
    assert book.total == 0
    assert "sh.601975" in portfolio


def test_symbols_tracks_created_books(portfolio: Portfolio) -> None:
    portfolio.book(SYM_B)
    assert set(portfolio.symbols()) == {SYMBOL, SYM_B}


def test_before_trading_rolls_all_books(portfolio: Portfolio) -> None:
    portfolio.book(SYM_B).before_trading(D0)
    portfolio.book(SYM_B).buy(D0, 100, 10.0)
    portfolio.before_trading(D1)
    assert portfolio.book(SYM_B).sellable == 100


def test_market_value_single_symbol(portfolio: Portfolio) -> None:
    book = portfolio.book(SYMBOL)
    book.before_trading(D0)
    book.buy(D0, 1000, 10.0)
    assert portfolio.market_value({SYMBOL: 12.0}) == pytest.approx(12_000.0)


def test_market_value_multi_symbol(portfolio: Portfolio) -> None:
    for sym, qty, px in ((SYMBOL, 1000, 10.0), (SYM_B, 500, 20.0)):
        book = portfolio.book(sym)
        book.before_trading(D0)
        book.buy(D0, qty, px)
    assert portfolio.market_value({SYMBOL: 11.0, SYM_B: 22.0}) == pytest.approx(
        1000 * 11.0 + 500 * 22.0
    )


def test_market_value_missing_price_raises(portfolio: Portfolio) -> None:
    """持仓非零却缺价格 → 报错，而不是静默按 0 估值。"""
    book = portfolio.book(SYMBOL)
    book.before_trading(D0)
    book.buy(D0, 1000, 10.0)
    with pytest.raises(KeyError, match=SYMBOL):
        portfolio.market_value({})


def test_flat_book_does_not_require_price(portfolio: Portfolio) -> None:
    portfolio.book(SYMBOL).before_trading(D0)
    assert portfolio.market_value({}) == 0.0


def test_held_symbols(portfolio: Portfolio) -> None:
    portfolio.book(SYMBOL).before_trading(D0)
    portfolio.book(SYM_B).before_trading(D0)
    portfolio.book(SYMBOL).buy(D0, 100, 10.0)
    assert portfolio.held_symbols() == (SYMBOL,)


def test_total_cost(portfolio: Portfolio) -> None:
    book = portfolio.book(SYMBOL)
    book.before_trading(D0)
    book.buy(D0, 1000, 10.0)
    assert portfolio.total_cost() == pytest.approx(10_000.0)


def test_unrealized_pnl_aggregates(portfolio: Portfolio) -> None:
    book = portfolio.book(SYMBOL)
    book.before_trading(D0)
    book.buy(D0, 1000, 10.0)
    trading, position = portfolio.unrealized_pnl({SYMBOL: 11.0})
    assert trading == pytest.approx(1000.0)
    assert position == pytest.approx(0.0)


def test_daily_pnl_breakdown_requires_prev_close(portfolio: Portfolio) -> None:
    book = portfolio.book(SYMBOL)
    book.before_trading(D0)
    book.buy(D0, 1000, 10.0)
    with pytest.raises(KeyError, match=SYMBOL):
        portfolio.daily_pnl_breakdown({SYMBOL: 11.0}, {})


def test_daily_pnl_breakdown_uses_prev_close(portfolio: Portfolio) -> None:
    book = portfolio.book(SYMBOL)
    book.before_trading(D0)
    book.buy(D0, 1000, 10.0)
    book.before_trading(D1)
    trading, position = portfolio.daily_pnl_breakdown(
        {SYMBOL: 12.0}, {SYMBOL: 11.0}
    )
    assert trading == pytest.approx(0.0)
    assert position == pytest.approx(1000.0)


def test_has_position(portfolio: Portfolio) -> None:
    assert not portfolio.has_position(SYMBOL)
    book = portfolio.book(SYMBOL)
    book.before_trading(D0)
    book.buy(D0, 100, 10.0)
    assert portfolio.has_position(SYMBOL)
