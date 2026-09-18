"""``core/backtest.py`` 单测 —— 含**确定性**与 **B&H 成本校验**两个核心门槛。

B&H 校验的意义：买入持有有**解析解**，回测净值与"手工持有"的差异必须能被
已知成本完全解释。对不上就说明规则层或撮合器有 bug。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from do_modle.core.backtest import BacktestEngine
from do_modle.core.recorder import BASE_FIELDS
from do_modle.objects import AdjustFlag, RejectReason, Side, Verdict
from do_modle.rules.cost import CostConfig
from tests._helpers import D0, D1, D2, MAIN_INSTRUMENT, SYMBOL, make_bar
from tests.fakes import FakeProvider

D3 = date(2026, 9, 17)
INITIAL_CASH = 200_000.0
QTY = 1000

# 手工构造的行情：涨跌停不触发（振幅均在 ±10% 内）
DEFAULT_BARS = [
    make_bar(d=D0, open_=10.0, close=10.0, prev_close=10.0),
    make_bar(d=D1, open_=10.1, close=10.2, prev_close=10.0),
    make_bar(d=D2, open_=10.3, close=10.5, prev_close=10.2),
    make_bar(d=D3, open_=10.4, close=10.6, prev_close=10.5),
]


def _provider(bars=None, calendar=None):
    bars = DEFAULT_BARS if bars is None else bars
    return FakeProvider(
        bars={(SYMBOL, AdjustFlag.NONE): bars},
        instruments={SYMBOL: MAIN_INSTRUMENT},
        calendar=calendar or [b.dt.date() for b in bars],
    )


def buy_and_hold(qty: int = QTY):
    """首个有 Bar 的交易日买入，之后不再操作。"""
    state = {"done": False}

    def handler(ctx):
        if state["done"] or ctx.bar is None:
            return None
        state["done"] = True
        return [ctx.order(Side.BUY, qty)]

    return handler


def _run(handler, *, bars=None, calendar=None, start=None, end=None, **kwargs):
    bars = DEFAULT_BARS if bars is None else bars
    # 起止区间优先取 calendar（可能包含无 Bar 的交易日）
    span = list(calendar) if calendar else [b.dt.date() for b in bars]
    engine = BacktestEngine(
        provider=_provider(bars, calendar),
        instrument=MAIN_INSTRUMENT,
        symbol=SYMBOL,
        handler=handler,
        start=start or min(span),
        end=end or max(span),
        initial_cash=INITIAL_CASH,
        **kwargs,
    )
    return engine.run()


# ==================== 核心门槛 1：确定性 ====================

def test_same_input_twice_gives_identical_nav() -> None:
    """同输入两次运行，净值必须**逐点相等**（不是 approx）。"""
    first = _run(buy_and_hold())
    second = _run(buy_and_hold())
    assert first.nav == second.nav
    assert first.nav_values == second.nav_values


def test_determinism_includes_trades_and_rejects() -> None:
    first = _run(buy_and_hold())
    second = _run(buy_and_hold())
    assert [(t.quantity, t.price) for t in first.trades] == [
        (t.quantity, t.price) for t in second.trades
    ]
    assert first.reject_reasons == second.reject_reasons


# ==================== 核心门槛 2：B&H 成本校验 ====================

def test_buy_and_hold_gap_is_explained_by_cost() -> None:
    """回测净值与"手工持有（零成本）"的差额必须**恰好等于滑点 + 费用**。"""
    result = _run(buy_and_hold())

    entry_price = 10.1  # D1 开盘价
    exec_price = 10.11  # 含买入滑点（至少 1 tick）
    exit_price = 10.6  # D3 收盘价

    ideal_final = INITIAL_CASH - QTY * entry_price + QTY * exit_price
    cfg = CostConfig()  # 从成本模型读费率，避免测试里硬编码
    fees = QTY * exec_price * cfg.commission_rate + QTY * exec_price * cfg.transfer_rate
    slippage = QTY * (exec_price - entry_price)

    assert ideal_final - result.final_nav == pytest.approx(fees + slippage, abs=1e-6)


def test_buy_and_hold_position_and_cash() -> None:
    result = _run(buy_and_hold())
    assert len(result.trades) == 1
    assert result.trades[0].quantity == QTY
    last = result.snapshots[-1]
    assert last.market_value == pytest.approx(QTY * 10.6)
    assert last.total == pytest.approx(result.final_nav)


def test_flat_strategy_keeps_initial_cash() -> None:
    """不下单 → 净值恒等于初始资金（无成本泄漏）。"""
    result = _run(lambda ctx: None)
    assert not result.trades
    assert all(v == pytest.approx(INITIAL_CASH) for v in result.nav_values)


# ==================== 执行时序 ====================

def test_signal_on_t_executes_on_t_plus_1() -> None:
    result = _run(buy_and_hold())
    trade = result.trades[0]
    assert trade.dt.date() == D1  # D0 出信号，D1 执行
    assert trade.raw_price == pytest.approx(10.1)  # D1 开盘价


def test_never_executes_on_signal_day() -> None:
    result = _run(buy_and_hold())
    assert all(t.dt.date() != D0 for t in result.trades)


def test_no_trade_when_calendar_has_single_day() -> None:
    """只有一天时，信号无次日可执行 → 不产生成交。"""
    bars = [make_bar(d=D0, open_=10.0, close=10.0, prev_close=10.0)]
    result = _run(buy_and_hold(), bars=bars)
    assert not result.trades


def test_suspended_next_day_rejects_day_order() -> None:
    """日单有效期为 1 个交易日：次日停牌 → 拒单，不跨日顺延。"""
    bars = [
        make_bar(d=D0, open_=10.0, close=10.0, prev_close=10.0),
        make_bar(d=D1, open_=10.0, close=10.0, prev_close=10.0, suspended=True),
        make_bar(d=D2, open_=10.3, close=10.5, prev_close=10.2),
    ]
    result = _run(buy_and_hold(), bars=bars)
    assert not result.trades
    assert result.reject_reasons.get(RejectReason.SUSPENDED.value) == 1


def test_order_not_carried_over_to_third_day() -> None:
    """停牌拒单后不应在 D2 补成交。"""
    bars = [
        make_bar(d=D0, open_=10.0, close=10.0, prev_close=10.0),
        make_bar(d=D1, open_=10.0, close=10.0, prev_close=10.0, suspended=True),
        make_bar(d=D2, open_=10.3, close=10.5, prev_close=10.2),
    ]
    result = _run(buy_and_hold(), bars=bars)
    assert all(t.dt.date() != D2 for t in result.trades)


# ==================== 净值序列与记录 ====================

def test_nav_length_matches_calendar() -> None:
    result = _run(buy_and_hold())
    assert len(result.nav) == len(DEFAULT_BARS)
    assert [d for d, _ in result.nav] == [b.dt.date() for b in DEFAULT_BARS]


def test_records_have_base_fields() -> None:
    result = _run(buy_and_hold())
    assert len(result.records) == len(DEFAULT_BARS)
    for row in result.records:
        for field in BASE_FIELDS:
            assert field in row, f"记录缺少字段 {field}"


def test_records_track_position_over_time() -> None:
    result = _run(buy_and_hold())
    assert [r["position"] for r in result.records] == [0, QTY, QTY, QTY]


def test_snapshot_count_matches_nav() -> None:
    result = _run(buy_and_hold())
    assert len(result.snapshots) == len(result.nav)


# ==================== 风控钩子 ====================

def test_pre_trade_hook_can_reject_order() -> None:
    """P6 的 RiskGate 从这里接入；被拒订单必须进入结果。"""
    result = _run(
        buy_and_hold(),
        pre_trade_hook=lambda order, ctx: Verdict.reject("TEST_BLOCK"),
    )
    assert not result.trades
    assert result.reject_reasons == {"TEST_BLOCK": 1}


def test_pre_trade_hook_passing_verdict_allows_order() -> None:
    result = _run(buy_and_hold(), pre_trade_hook=lambda order, ctx: Verdict.ok())
    assert len(result.trades) == 1


# ==================== 事件总线接入 ====================

def test_events_published() -> None:
    from do_modle.core.event import EventBus, EventType

    bus = EventBus()
    counts: dict[str, int] = {}
    for etype in (EventType.BEFORE_TRADING, EventType.BAR, EventType.AFTER_TRADING):
        bus.subscribe(etype, lambda e, t=etype: counts.__setitem__(t, counts.get(t, 0) + 1))

    _run(buy_and_hold(), bus=bus)
    assert counts[EventType.BEFORE_TRADING] == len(DEFAULT_BARS)
    assert counts[EventType.BAR] == len(DEFAULT_BARS)
    assert counts[EventType.AFTER_TRADING] == len(DEFAULT_BARS)


def test_trade_event_published() -> None:
    from do_modle.core.event import EventBus, EventType

    bus = EventBus()
    seen: list[object] = []
    bus.subscribe(EventType.TRADE, lambda e: seen.append(e.payload))
    _run(buy_and_hold(), bus=bus)
    assert len(seen) == 1


# ==================== 边界 ====================

def test_calendar_without_bars_keeps_nav_flat() -> None:
    """日历有交易日但无 Bar（长期停牌）→ 不交易、净值不变。"""
    calendar = [D0, D1, D2]
    bars = [make_bar(d=D0, open_=10.0, close=10.0, prev_close=10.0)]
    result = _run(buy_and_hold(), bars=bars, calendar=calendar)
    assert len(result.nav) == 3
    assert not result.trades


def test_reject_reasons_summary_counts() -> None:
    bars = [
        make_bar(d=D0, open_=10.0, close=10.0, prev_close=10.0),
        make_bar(d=D1, open_=10.0, close=10.0, prev_close=10.0, suspended=True),
    ]
    result = _run(buy_and_hold(), bars=bars)
    assert sum(result.reject_reasons.values()) == len(result.rejects)


def test_result_helpers() -> None:
    result = _run(buy_and_hold())
    assert result.final_nav > 0
    assert result.total_fee > 0
    assert result.nav_values[-1] == result.final_nav
