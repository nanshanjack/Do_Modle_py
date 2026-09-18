"""``evaluation/reconcile.py`` 单测 —— 对账逻辑（多数用例不需要 akquant）。"""

from __future__ import annotations

from datetime import date

import pytest

from do_modle.evaluation.reconcile import (
    KNOWN_DIFF,
    MATCH,
    MISMATCH,
    NON_RECONCILE_SCOPE,
    RECONCILE_SCOPE,
    SKIP,
    ReconcileCase,
    ReconcileEngine,
    ReconcileReport,
    akquant_available,
)
from do_modle.objects import AdjustFlag, Side
from do_modle.rules.cost import AShareCostModel, CostConfig
from tests._helpers import MAIN_INSTRUMENT, SYMBOL, make_bar
from tests.fakes import FakeProvider

D0 = date(2026, 9, 14)
D1 = date(2026, 9, 15)
D2 = date(2026, 9, 16)

ENGINE = ReconcileEngine()


def _series():
    return [
        make_bar(d=D0, open_=20.0, close=20.0, prev_close=20.0, volume=1_000_000),
        make_bar(d=D1, open_=20.0, close=20.1, prev_close=20.0, volume=1_000_000),
        make_bar(d=D2, open_=20.1, close=20.2, prev_close=20.1, volume=1_000_000),
    ]


# ==================== 范围声明 ====================

def test_scope_declares_seven_reconcilable_items() -> None:
    assert len(RECONCILE_SCOPE) == 7
    joined = " ".join(RECONCILE_SCOPE)
    for key in ("成本", "滑点", "价格基准", "tick", "手数", "资金", "T+1"):
        assert key in joined


def test_scope_declares_three_non_reconcilable_items() -> None:
    assert len(NON_RECONCILE_SCOPE) == 3
    joined = " ".join(NON_RECONCILE_SCOPE)
    for key in ("涨跌停", "一字板", "流动性"):
        assert key in joined


# ==================== 报告结构 ====================

def test_report_counts() -> None:
    rep = ReconcileReport()
    rep.add(ReconcileCase("a", MATCH, "ok"))
    rep.add(ReconcileCase("b", MISMATCH, "bad"))
    rep.add(ReconcileCase("c", SKIP, "n/a"))
    assert len(rep.matched) == 1
    assert len(rep.mismatched) == 1
    assert len(rep.skipped) == 1
    assert rep.ok is False


def test_report_ok_when_no_mismatch() -> None:
    rep = ReconcileReport()
    rep.add(ReconcileCase("a", MATCH, "ok"))
    rep.add(ReconcileCase("b", SKIP, "n/a"))
    assert rep.ok is True


def test_report_format_text() -> None:
    rep = ReconcileReport()
    rep.add(ReconcileCase("正常订单", MATCH, "一致"))
    text = rep.format_text()
    assert "正常订单" in text
    assert "一致 1" in text


# ==================== 比较逻辑（不依赖 akquant） ====================

def _ours(**kw) -> dict:
    base = {
        "filled": 1,
        "quantity": 1000,
        "price": 20.01,
        "commission": 2.001,
        "transfer_fee": 0.2001,
        "reject": None,
    }
    base.update(kw)
    return base


def _theirs(**kw) -> dict:
    base = {"filled": 1, "quantity": 1000, "price": 20.01, "commission": 2.2011}
    base.update(kw)
    return base


def test_compare_match() -> None:
    case = ENGINE._compare("正常订单", _ours(), _theirs())
    assert case.status == MATCH


def test_compare_accepts_transfer_fee_merged_into_commission() -> None:
    """akquant 把过户费并入 commission —— 2.001 + 0.2001 = 2.2011。"""
    case = ENGINE._compare("费用口径", _ours(), _theirs(commission=2.2011))
    assert case.status == MATCH


def test_compare_detects_price_mismatch() -> None:
    case = ENGINE._compare("价格", _ours(), _theirs(price=20.05))
    assert case.status == MISMATCH
    assert "成交价" in case.detail


def test_compare_detects_quantity_mismatch() -> None:
    case = ENGINE._compare("数量", _ours(), _theirs(quantity=500))
    assert case.status == MISMATCH
    assert "成交数量" in case.detail


def test_compare_detects_fee_mismatch() -> None:
    case = ENGINE._compare("费用", _ours(), _theirs(commission=9.99))
    assert case.status == MISMATCH
    assert "费用总额" in case.detail


def test_compare_detects_fill_count_mismatch() -> None:
    case = ENGINE._compare("笔数", _ours(), _theirs(filled=0))
    assert case.status == MISMATCH
    assert "成交笔数" in case.detail


def test_compare_both_unfilled_is_match() -> None:
    case = ENGINE._compare(
        "无成交", _ours(filled=0, quantity=0, price=None), _theirs(filled=0)
    )
    assert case.status == MATCH


def test_tolerance_is_configurable() -> None:
    loose = ReconcileEngine(tolerance=0.1)
    assert loose._compare("松容差", _ours(), _theirs(price=20.05)).status == MATCH


# ==================== 已知模型差异：滑点取整 ====================

def test_slippage_rounding_diff_is_known_diff_not_mismatch() -> None:
    """自建『至少 1 tick』vs akquant『纯百分比不取整』——已知模型差异，不算失败。

    实测：6.08 元上自建 6.09（1 tick），akquant 6.08304（0.05% 精确值）。
    """
    ours = _ours(price=6.09, commission=0.609, transfer_fee=0.0609)
    theirs = _theirs(price=6.08304, commission=0.6691344)
    case = ENGINE._compare("低价股", ours, theirs)
    assert case.status == KNOWN_DIFF
    assert "滑点模型差异" in case.detail


def test_known_diff_does_not_fail_report() -> None:
    rep = ReconcileReport()
    rep.add(ReconcileCase("低价股", KNOWN_DIFF, "滑点取整"))
    assert rep.ok is True
    assert len(rep.known_diffs) == 1


def test_price_diff_beyond_one_tick_is_mismatch() -> None:
    """价差超过 1 tick 就不是滑点取整能解释的了。"""
    case = ENGINE._compare("真差异", _ours(price=20.01), _theirs(price=20.50))
    assert case.status == MISMATCH


def test_aligned_price_difference_is_mismatch() -> None:
    """两边都对齐 tick 但数值不同 → 真差异。"""
    case = ENGINE._compare("对齐价差", _ours(price=20.01), _theirs(price=20.02))
    assert case.status == MISMATCH


def test_slippage_rounding_detector() -> None:
    assert ENGINE._is_slippage_rounding_diff(6.09, 6.08304) is True
    assert ENGINE._is_slippage_rounding_diff(6.09, 6.09) is False
    assert ENGINE._is_slippage_rounding_diff(6.09, 6.50) is False


# ==================== 自建拒绝 → 跳过 ====================

def test_ours_reject_leads_to_skip() -> None:
    """自建因涨跌停拒绝时，该场景不可对账，应 SKIP 而非 MISMATCH。"""
    bar = make_bar(d=D1, open_=22.0, prev_close=20.0)  # 涨停
    series = [
        make_bar(d=D0, open_=20.0, close=20.0, prev_close=20.0),
        bar,
    ]
    case = ENGINE.reconcile_order(
        name="涨停买单",
        bar=bar,
        side=Side.BUY,
        quantity=100,
        instrument=MAIN_INSTRUMENT,
        series=series,
        signal_date=D0,
    )
    assert case.status == SKIP
    assert "涨跌停" in case.detail or "LIMIT_UP" in case.detail


def test_ours_suspension_leads_to_skip() -> None:
    bar = make_bar(d=D1, open_=20.0, prev_close=20.0, suspended=True)
    series = [make_bar(d=D0, open_=20.0, close=20.0, prev_close=20.0), bar]
    case = ENGINE.reconcile_order(
        name="停牌买单",
        bar=bar,
        side=Side.BUY,
        quantity=100,
        instrument=MAIN_INSTRUMENT,
        series=series,
        signal_date=D0,
    )
    assert case.status == SKIP


# ==================== 端到端（需 akquant） ====================

def test_akquant_available_flag() -> None:
    assert isinstance(akquant_available(), bool)


@pytest.mark.skipif(not akquant_available(), reason="未安装 akquant")
def test_end_to_end_normal_order_matches() -> None:
    """正常订单：两引擎的成交价与费用总额应完全一致。"""
    series = _series()
    case = ENGINE.reconcile_order(
        name="正常买单 1000 股",
        bar=series[1],
        side=Side.BUY,
        quantity=1000,
        instrument=MAIN_INSTRUMENT,
        series=series,
        signal_date=D0,
    )
    assert case.status == MATCH, case.detail
    assert case.ours["price"] == pytest.approx(20.01)


@pytest.mark.skipif(not akquant_available(), reason="未安装 akquant")
def test_end_to_end_large_order_matches_after_clipping() -> None:
    """大额订单：自建先裁剪，再把裁剪后的订单喂给 akquant。"""
    series = [
        make_bar(d=D0, open_=20.0, close=20.0, prev_close=20.0, volume=1_000_000),
        make_bar(d=D1, open_=20.0, close=20.1, prev_close=20.0, volume=10_000),
        make_bar(d=D2, open_=20.1, close=20.2, prev_close=20.1, volume=1_000_000),
    ]
    case = ENGINE.reconcile_order(
        name="超额订单 5000 股",
        bar=series[1],
        side=Side.BUY,
        quantity=5000,
        instrument=MAIN_INSTRUMENT,
        series=series,
        signal_date=D0,
    )
    # 自建裁剪到 1000 股；akquant 收到的也是 1000 股
    assert case.ours["quantity"] == 1000
    assert case.status == MATCH, case.detail


def test_missing_akquant_leads_to_skip(monkeypatch) -> None:
    """未安装 akquant 时 SKIP，而不是崩溃。"""
    import do_modle.evaluation.reconcile as rec

    monkeypatch.setattr(rec, "akquant_available", lambda: False)
    engine = rec.ReconcileEngine()
    series = _series()
    case = engine.reconcile_order(
        name="无 akquant",
        bar=series[1],
        side=Side.BUY,
        quantity=1000,
        instrument=MAIN_INSTRUMENT,
        series=series,
        signal_date=D0,
    )
    assert case.status == SKIP
    assert "akquant" in case.detail
