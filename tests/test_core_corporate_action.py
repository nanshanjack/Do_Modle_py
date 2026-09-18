"""P3.5：公司行为接入引擎 —— 送转 + 现金分红。

**为什么必须在除权日盘前应用**：股权登记日是 T-1，除权除息日是 T。
持有到 T-1 收盘的账户，在 T 日开盘前即已享有送转股与分红现金。

**已知简化**：现金分红按**税前**计入。个人红利税实行差异化征收
（持股 >1 年免征，1 个月~1 年减按 50% 计入，<1 个月全额计入），
且**在卖出时补缴**，与持有期挂钩。精确建模会显著增加归因复杂度。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from do_modle.core.backtest import BacktestEngine
from do_modle.core.event import EventBus, EventType
from do_modle.objects import AdjustFlag, CorporateAction, Side
from do_modle.rules.account import Account
from do_modle.rules.portfolio import Portfolio
from tests._helpers import D0, D1, D2, MAIN_INSTRUMENT, SYMBOL, make_bar
from tests.fakes import FakeProvider

D3 = date(2026, 9, 17)
INITIAL_CASH = 200_000.0


def _bars():
    """4 个交易日，价格平稳（不触发涨跌停）。"""
    return [
        make_bar(d=D0, open_=10.0, close=10.0, prev_close=10.0),
        make_bar(d=D1, open_=10.0, close=10.0, prev_close=10.0),
        make_bar(d=D2, open_=10.0, close=10.0, prev_close=10.0),
        make_bar(d=D3, open_=10.0, close=10.0, prev_close=10.0),
    ]


def _run(*, dividends=None, actions_enabled=True, qty=1000):
    bars = _bars()
    provider = FakeProvider(
        bars={(SYMBOL, AdjustFlag.NONE): bars},
        instruments={SYMBOL: MAIN_INSTRUMENT},
        calendar=[b.dt.date() for b in bars],
        dividends={SYMBOL: dividends or []},
    )
    state = {"done": False}

    def handler(ctx):
        if state["done"] or ctx.bar is None:
            return None
        state["done"] = True
        return [ctx.order(Side.BUY, qty)]

    engine = BacktestEngine(
        provider=provider,
        instrument=MAIN_INSTRUMENT,
        symbol=SYMBOL,
        handler=handler,
        start=D0,
        end=D3,
        initial_cash=INITIAL_CASH,
        apply_corporate_actions=actions_enabled,
    )
    return engine.run()


# ==================== Ledger 层 ====================

def test_ledger_applies_cash_dividend() -> None:
    from do_modle.core.ledger import Ledger

    ledger = Ledger(Portfolio([SYMBOL]), Account(initial_cash=100_000.0))
    ledger.on_before_trading(D0)
    book = ledger.portfolio.book(SYMBOL)
    book.buy(D0, 1000, 10.0)

    cash = ledger.on_corporate_action(CorporateAction(SYMBOL, D1, cash_per_share=0.5))
    assert cash == pytest.approx(500.0)
    assert ledger.cash == pytest.approx(100_500.0)
    assert ledger.dividend_cash == pytest.approx(500.0)


def test_ledger_applies_share_ratio() -> None:
    from do_modle.core.ledger import Ledger

    ledger = Ledger(Portfolio([SYMBOL]), Account(initial_cash=100_000.0))
    ledger.on_before_trading(D0)
    ledger.portfolio.book(SYMBOL).buy(D0, 1000, 10.0)

    ledger.on_corporate_action(CorporateAction(SYMBOL, D1, share_ratio=0.3))
    assert ledger.position(SYMBOL) == 1300
    assert ledger.portfolio.book(SYMBOL).avg_cost == pytest.approx(10.0 / 1.3)


def test_ledger_counts_corporate_actions() -> None:
    from do_modle.core.ledger import Ledger

    ledger = Ledger(Portfolio([SYMBOL]), Account(initial_cash=100_000.0))
    ledger.on_before_trading(D0)
    ledger.on_corporate_action(CorporateAction(SYMBOL, D1, cash_per_share=0.1))
    ledger.on_corporate_action(CorporateAction(SYMBOL, D2, cash_per_share=0.1))
    assert ledger.corporate_action_count == 2


def test_account_deposit_rejects_negative() -> None:
    acc = Account(initial_cash=1000.0)
    with pytest.raises(ValueError):
        acc.deposit(-1.0)


# ==================== 引擎层 ====================

def test_cash_dividend_reaches_account() -> None:
    """分红现金必须进入账户，净值因此上升。"""
    result = _run(dividends=[CorporateAction(SYMBOL, D2, cash_per_share=0.5)])
    assert result.dividend_cash == pytest.approx(500.0)
    assert len(result.corporate_actions) == 1

    baseline = _run(dividends=[])
    assert result.final_nav - baseline.final_nav == pytest.approx(500.0)


def test_dividend_on_execution_day_is_not_entitled() -> None:
    """**A股除权语义**：股权登记日是 T-1，除权除息日是 T。

    D1 买入的股票**不享有** D1 的分红。引擎在 D1 盘前先应用公司行为、
    再执行订单，因此此刻持仓仍为 0 —— 这个顺序是刻意的。
    """
    result = _run(dividends=[CorporateAction(SYMBOL, D1, cash_per_share=0.5)])
    assert result.dividend_cash == 0.0
    assert result.records[-1]["position"] == 1000  # 买入仍然成交


def test_dividend_after_position_established_is_entitled() -> None:
    """持仓建立后（D2 起）的分红才享有。"""
    result = _run(dividends=[CorporateAction(SYMBOL, D3, cash_per_share=0.5)])
    assert result.dividend_cash == pytest.approx(500.0)


def test_share_ratio_increases_position() -> None:
    result = _run(dividends=[CorporateAction(SYMBOL, D2, share_ratio=0.3)])
    assert result.records[-1]["position"] == 1300


def test_share_ratio_keeps_market_value_after_dilution() -> None:
    """送转后数量增加、成本摊薄，市值不变（价格未变时）。"""
    with_ratio = _run(dividends=[CorporateAction(SYMBOL, D2, share_ratio=0.3)])
    without = _run(dividends=[])
    # 1000 股 → 1300 股，价格仍为 10.0；净值差 = 300 股 × 10 元 = 3000
    assert with_ratio.final_nav - without.final_nav == pytest.approx(3000.0)


def test_corporate_action_disabled() -> None:
    """``apply_corporate_actions=False`` 时忽略全部公司行为。"""
    result = _run(
        dividends=[CorporateAction(SYMBOL, D1, cash_per_share=0.5)], actions_enabled=False
    )
    assert result.dividend_cash == 0.0
    assert result.corporate_actions == []
    assert result.records[-1]["position"] == 1000


def test_corporate_action_event_published() -> None:
    bus = EventBus()
    seen: list[object] = []
    bus.subscribe(EventType.CORPORATE_ACTION, lambda e: seen.append(e.payload))

    bars = _bars()
    provider = FakeProvider(
        bars={(SYMBOL, AdjustFlag.NONE): bars},
        instruments={SYMBOL: MAIN_INSTRUMENT},
        calendar=[b.dt.date() for b in bars],
        dividends={SYMBOL: [CorporateAction(SYMBOL, D1, cash_per_share=0.5)]},
    )
    engine = BacktestEngine(
        provider=provider,
        instrument=MAIN_INSTRUMENT,
        symbol=SYMBOL,
        handler=lambda ctx: None,
        start=D0,
        end=D3,
        initial_cash=INITIAL_CASH,
        bus=bus,
    )
    engine.run()
    assert len(seen) == 1


def test_no_dividends_is_noop() -> None:
    result = _run(dividends=[])
    assert result.dividend_cash == 0.0
    assert result.corporate_actions == []
    assert result.records[-1]["position"] == 1000


def test_multiple_actions_on_same_day() -> None:
    """同日多条公司行为（如既送转又分红）应全部应用。"""
    result = _run(
        dividends=[
            CorporateAction(SYMBOL, D2, share_ratio=0.3),
            CorporateAction(SYMBOL, D2, cash_per_share=0.2),
        ]
    )
    assert len(result.corporate_actions) == 2
    assert result.records[-1]["position"] == 1300


def test_action_before_position_is_bought_is_noop() -> None:
    """除权日早于建仓日 → 无持仓，公司行为不产生影响。"""
    result = _run(dividends=[CorporateAction(SYMBOL, D0, cash_per_share=0.5)])
    # D0 是信号日，D1 才成交；D0 盘前无持仓
    assert result.dividend_cash == 0.0


def test_provider_without_dividends_support_is_tolerated() -> None:
    """数据源不支持分红（默认返回空列表）时不应报错。"""

    class NoDividendProvider(FakeProvider):
        def get_dividends(self, symbol, start, end):
            raise NotImplementedError

    bars = _bars()
    provider = NoDividendProvider(
        bars={(SYMBOL, AdjustFlag.NONE): bars},
        instruments={SYMBOL: MAIN_INSTRUMENT},
        calendar=[b.dt.date() for b in bars],
    )
    engine = BacktestEngine(
        provider=provider,
        instrument=MAIN_INSTRUMENT,
        symbol=SYMBOL,
        handler=lambda ctx: None,
        start=D0,
        end=D3,
        initial_cash=INITIAL_CASH,
    )
    result = engine.run()
    assert result.corporate_actions == []


def test_dividend_cash_survives_determinism() -> None:
    divs = [CorporateAction(SYMBOL, D1, cash_per_share=0.5)]
    first, second = _run(dividends=divs), _run(dividends=divs)
    assert first.nav == second.nav
    assert first.dividend_cash == second.dividend_cash
