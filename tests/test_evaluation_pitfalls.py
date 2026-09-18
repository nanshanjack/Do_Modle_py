"""``evaluation/pitfalls.py`` 单测 —— 10 项陷阱自检。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from do_modle.evaluation.pitfalls import (
    PITFALL_NAMES,
    CheckStatus,
    PitfallReport,
    bonferroni_z,
    run_pitfall_checks,
)
from do_modle.objects import AdjustFlag, Side, Trade
from do_modle.rules.cost import AShareCostModel, CostConfig
from tests._helpers import D0, D1, D2, MAIN_INSTRUMENT, SYMBOL, make_bar
from tests.fakes import FakeProvider

D3 = date(2026, 9, 17)


def _trade(d: date, price: float, raw: float, qty: int = 1000, side=Side.BUY) -> Trade:
    return Trade(
        symbol=SYMBOL,
        dt=datetime(d.year, d.month, d.day, 9, 30),
        side=side,
        quantity=qty,
        price=price,
        raw_price=raw,
        commission=price * qty * 0.0001,
        stamp_tax=0.0 if side is Side.BUY else price * qty * 0.0005,
        transfer_fee=price * qty * 0.00001,
        slippage_cost=qty * abs(price - raw),
        cash_flow=-(price * qty),
    )


def _result(trades=(), rejects=(), records=None, dates=None):
    dates = dates or [D0, D1, D2, D3]
    records = records or [{"date": d, "position": 0} for d in dates]
    return SimpleNamespace(trades=list(trades), rejects=list(rejects), records=records)


def _bars(dates=None, suspended=()):
    dates = dates or [D0, D1, D2, D3]
    return [
        make_bar(d=d, open_=10.0, close=10.0, prev_close=10.0, suspended=d in suspended)
        for d in dates
    ]


COST = CostConfig()


# ==================== bonferroni_z ====================

@pytest.mark.parametrize(
    ("n", "expected"),
    [(1, 1.960), (2, 2.241), (5, 2.576), (10, 2.807), (20, 3.023), (50, 3.291), (100, 3.481)],
)
def test_bonferroni_z_table(n: int, expected: float) -> None:
    assert bonferroni_z(n) == pytest.approx(expected)


def test_bonferroni_z_interpolates() -> None:
    z3 = bonferroni_z(3)
    assert bonferroni_z(2) < z3 < bonferroni_z(5)


def test_bonferroni_z_monotonic() -> None:
    zs = [bonferroni_z(n) for n in (1, 2, 5, 10, 20, 50, 100, 500)]
    assert zs == sorted(zs)


def test_bonferroni_z_invalid() -> None:
    with pytest.raises(ValueError):
        bonferroni_z(0)


# ==================== 报告结构 ====================

def test_report_contains_all_ten_checks() -> None:
    rep = run_pitfall_checks(_result(), bars=_bars(), end=D3, cost_config=COST)
    assert len(rep.results) == 10
    assert [r.name for r in rep.results] == list(PITFALL_NAMES)


def test_report_helpers() -> None:
    rep = run_pitfall_checks(_result(), bars=_bars(), end=D3, cost_config=COST)
    # 4 天区间会正确触发第 8 项（回撤期外推）失败
    assert rep.ok is False
    assert len(rep.failures) == 1
    assert rep.failures[0].name == PITFALL_NAMES[7]
    from do_modle.evaluation.pitfalls import CheckResult

    assert isinstance(rep.by_name(PITFALL_NAMES[0]), CheckResult)
    assert rep.by_name("不存在的检查项") is None


def test_format_text_lists_all_checks() -> None:
    text = run_pitfall_checks(_result(), bars=_bars(), end=D3).format_text()
    for name in PITFALL_NAMES:
        assert name in text


def test_empty_report_add_and_status() -> None:
    rep = PitfallReport()
    rep.add("x", CheckStatus.FAIL, "boom")
    assert rep.ok is False
    assert len(rep.failures) == 1


# ==================== 各项检查 ====================

def test_skip_when_no_bars_provided() -> None:
    rep = run_pitfall_checks(_result(), end=D3)
    assert rep.by_name(PITFALL_NAMES[0]).status is CheckStatus.SKIP
    assert rep.by_name(PITFALL_NAMES[1]).status is CheckStatus.SKIP


def test_pitfall_1_pass_when_trades_match_bars() -> None:
    trades = [_trade(D1, 10.01, 10.0)]
    rep = run_pitfall_checks(_result(trades), bars=_bars(), end=D3, cost_config=COST)
    assert rep.by_name(PITFALL_NAMES[0]).status is CheckStatus.PASS


def test_pitfall_1_fail_when_trade_outside_bars() -> None:
    outside = D3 + timedelta(days=10)
    trades = [_trade(outside, 10.01, 10.0)]
    rep = run_pitfall_checks(_result(trades), bars=_bars(), end=D3, cost_config=COST)
    assert rep.by_name(PITFALL_NAMES[0]).status is CheckStatus.FAIL


def test_pitfall_2_fail_on_same_day_trade() -> None:
    """首日成交 = 信号日与成交日同日 → 未来函数。"""
    trades = [_trade(D0, 10.01, 10.0)]
    rep = run_pitfall_checks(_result(trades), bars=_bars(), end=D3, cost_config=COST)
    assert rep.by_name(PITFALL_NAMES[1]).status is CheckStatus.FAIL
    assert "首个交易日" in rep.by_name(PITFALL_NAMES[1]).detail


def test_pitfall_2_fail_when_price_not_open() -> None:
    trades = [_trade(D1, 10.01, 9.85)]  # raw_price 不等于当日开盘 10.0
    rep = run_pitfall_checks(_result(trades), bars=_bars(), end=D3, cost_config=COST)
    assert rep.by_name(PITFALL_NAMES[1]).status is CheckStatus.FAIL
    assert "开盘价" in rep.by_name(PITFALL_NAMES[1]).detail


def test_pitfall_2_pass_for_correct_execution() -> None:
    trades = [_trade(D1, 10.01, 10.0)]
    rep = run_pitfall_checks(_result(trades), bars=_bars(), end=D3, cost_config=COST)
    assert rep.by_name(PITFALL_NAMES[1]).status is CheckStatus.PASS


def test_pitfall_3_fail_when_last_bar_far_from_end() -> None:
    """标的疑似提前停牌/退市 → 幸存者偏差风险。"""
    bars = _bars(dates=[D0, D1])
    far_end = D1 + timedelta(days=60)
    rep = run_pitfall_checks(_result(), bars=bars, end=far_end, cost_config=COST)
    assert rep.by_name(PITFALL_NAMES[2]).status is CheckStatus.FAIL
    assert "退市" in rep.by_name(PITFALL_NAMES[2]).detail


def test_pitfall_3_tolerates_short_gap() -> None:
    bars = _bars(dates=[D0, D1])
    rep = run_pitfall_checks(
        _result(), bars=bars, end=D1 + timedelta(days=5), cost_config=COST
    )
    assert rep.by_name(PITFALL_NAMES[2]).status is CheckStatus.PASS


def test_pitfall_3_pass_when_bar_reaches_end() -> None:
    rep = run_pitfall_checks(_result(), bars=_bars(), end=D3, cost_config=COST)
    assert rep.by_name(PITFALL_NAMES[2]).status is CheckStatus.PASS


def test_pitfall_4_warn_without_in_sample_end() -> None:
    rep = run_pitfall_checks(_result(), bars=_bars(), end=D3)
    assert rep.by_name(PITFALL_NAMES[3]).status is CheckStatus.WARN


def test_pitfall_4_pass_with_in_sample_end() -> None:
    rep = run_pitfall_checks(_result(), bars=_bars(), end=D3, in_sample_end="2021-12-31")
    assert rep.by_name(PITFALL_NAMES[3]).status is CheckStatus.PASS


def test_pitfall_5_skip_for_single_trial() -> None:
    rep = run_pitfall_checks(_result(), bars=_bars(), end=D3, n_trials=1)
    assert rep.by_name(PITFALL_NAMES[4]).status is CheckStatus.SKIP


def test_pitfall_5_warn_with_adjusted_threshold() -> None:
    rep = run_pitfall_checks(_result(), bars=_bars(), end=D3, n_trials=20)
    result = rep.by_name(PITFALL_NAMES[4])
    assert result.status is CheckStatus.WARN
    assert "3.023" in result.detail


def test_pitfall_6_fail_when_slippage_zero() -> None:
    rep = run_pitfall_checks(
        _result([_trade(D1, 10.0, 10.0)]),
        bars=_bars(),
        end=D3,
        cost_config=CostConfig(slippage=0.0),
    )
    assert rep.by_name(PITFALL_NAMES[5]).status is CheckStatus.FAIL
    assert "滑点为 0" in rep.by_name(PITFALL_NAMES[5]).detail


def test_pitfall_6_fail_when_trades_have_no_slippage() -> None:
    """成交价 = 开盘价 → 滑点被吃掉（模型 B 之前的典型症状）。"""
    rep = run_pitfall_checks(
        _result([_trade(D1, 10.0, 10.0)]), bars=_bars(), end=D3, cost_config=COST
    )
    assert rep.by_name(PITFALL_NAMES[5]).status is CheckStatus.FAIL


def test_pitfall_6_pass_with_slippage() -> None:
    rep = run_pitfall_checks(
        _result([_trade(D1, 10.01, 10.0)]), bars=_bars(), end=D3, cost_config=COST
    )
    assert rep.by_name(PITFALL_NAMES[5]).status is CheckStatus.PASS


def test_pitfall_6_skip_when_no_trades() -> None:
    rep = run_pitfall_checks(_result(), bars=_bars(), end=D3, cost_config=COST)
    assert rep.by_name(PITFALL_NAMES[5]).status is CheckStatus.SKIP


def test_pitfall_7_is_always_manual_warn() -> None:
    rep = run_pitfall_checks(_result(), bars=_bars(), end=D3)
    assert rep.by_name(PITFALL_NAMES[6]).status is CheckStatus.WARN


def test_pitfall_8_fail_when_span_too_short() -> None:
    rep = run_pitfall_checks(_result(), bars=_bars(), end=D3)
    assert rep.by_name(PITFALL_NAMES[7]).status is CheckStatus.FAIL
    assert "4" in rep.by_name(PITFALL_NAMES[7]).detail


def test_pitfall_8_pass_for_long_span() -> None:
    dates = [date(2016, 1, 4) + timedelta(days=365 * i) for i in range(6)]
    records = [{"date": d, "position": 0} for d in dates]
    bars = [
        make_bar(d=d, open_=10.0, close=10.0, prev_close=10.0) for d in dates
    ]
    rep = run_pitfall_checks(
        _result(records=records), bars=bars, end=dates[-1], cost_config=COST
    )
    assert rep.by_name(PITFALL_NAMES[7]).status is CheckStatus.PASS


def test_pitfall_9_fail_on_trade_during_suspension() -> None:
    bars = _bars(suspended={D1})
    trades = [_trade(D1, 10.01, 10.0)]
    rep = run_pitfall_checks(_result(trades), bars=bars, end=D3, cost_config=COST)
    assert rep.by_name(PITFALL_NAMES[8]).status is CheckStatus.FAIL
    assert "停牌日" in rep.by_name(PITFALL_NAMES[8]).detail


def test_pitfall_9_pass_and_reports_suspension_count() -> None:
    bars = _bars(suspended={D1})
    rep = run_pitfall_checks(_result(), bars=bars, end=D3, cost_config=COST)
    result = rep.by_name(PITFALL_NAMES[8])
    assert result.status is CheckStatus.PASS
    assert "1 个停牌日" in result.detail


def test_pitfall_10_is_skipped_to_p7() -> None:
    rep = run_pitfall_checks(_result(), bars=_bars(), end=D3)
    result = rep.by_name(PITFALL_NAMES[9])
    assert result.status is CheckStatus.SKIP
    assert "P7" in result.detail


# ==================== 端到端：真实引擎输出 ====================


def test_end_to_end_with_real_engine() -> None:
    """用真实引擎跑一遍，清单应无 FAIL（除了区间长度）。"""
    from do_modle.core.backtest import BacktestEngine

    bars = [
        make_bar(d=D0, open_=10.0, close=10.0, prev_close=10.0),
        make_bar(d=D1, open_=10.1, close=10.2, prev_close=10.0),
        make_bar(d=D2, open_=10.3, close=10.5, prev_close=10.2),
        make_bar(d=D3, open_=10.4, close=10.6, prev_close=10.5),
    ]
    provider = FakeProvider(
        bars={(SYMBOL, AdjustFlag.NONE): bars},
        instruments={SYMBOL: MAIN_INSTRUMENT},
        calendar=[b.dt.date() for b in bars],
    )
    state = {"done": False}

    def handler(ctx):
        if state["done"] or ctx.bar is None:
            return None
        state["done"] = True
        return [ctx.order(Side.BUY, 1000)]

    result = BacktestEngine(
        provider=provider,
        instrument=MAIN_INSTRUMENT,
        symbol=SYMBOL,
        handler=handler,
        start=D0,
        end=D3,
        initial_cash=200_000.0,
    ).run()

    rep = run_pitfall_checks(
        result,
        bars=bars,
        end=D3,
        in_sample_end="2021-12-31",
        cost_config=CostConfig(),
    )
    # 1/2/3/6/9 必须通过；8 会因区间太短而 FAIL（预期）
    for idx in (0, 1, 2, 5, 8):
        assert rep.by_name(PITFALL_NAMES[idx]).status is CheckStatus.PASS, (
            f"{PITFALL_NAMES[idx]} 未通过：{rep.by_name(PITFALL_NAMES[idx]).detail}"
        )
    assert rep.by_name(PITFALL_NAMES[7]).status is CheckStatus.FAIL
    assert rep.ok is False
