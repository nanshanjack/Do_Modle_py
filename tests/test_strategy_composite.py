"""``strategy/composite.py`` 单测 —— 多因子合成。

**核心纪律**：
* 横截面标准化（不同因子量纲差异巨大，不标准化则量纲大的主导）
* 只做等权（不做参数拟合 → 不引入过拟合）
* 缺失标的按可用因子归一化权重（不因个别字段缺失丢掉整个标的）
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from do_modle.strategy.composite import (
    combine_panels,
    combine_ranks,
    panel_correlation,
    rank_cross_section,
    zscore_cross_section,
)

D0 = date(2026, 9, 14)
D1 = date(2026, 9, 15)


def _panel(values: dict, d: date = D0):
    return {d: dict(values)}


# ==================== 横截面标准化 ====================

def test_zscore_mean_zero_std_one() -> None:
    out = zscore_cross_section(_panel({"a": 1.0, "b": 2.0, "c": 3.0}))
    zs = list(out[D0].values())
    assert sum(zs) == pytest.approx(0.0, abs=1e-12)
    assert sum(x * x for x in zs) / (len(zs) - 1) == pytest.approx(1.0)


def test_zscore_preserves_order() -> None:
    out = zscore_cross_section(_panel({"a": 1.0, "b": 5.0, "c": 3.0}))
    assert out[D0]["a"] < out[D0]["c"] < out[D0]["b"]


def test_zscore_constant_series_gives_zero() -> None:
    """标准差为 0（全部相同）→ 无信息，置 0 而非除零。"""
    out = zscore_cross_section(_panel({"a": 1.0, "b": 1.0, "c": 1.0}))
    assert out[D0] == {"a": 0.0, "b": 0.0, "c": 0.0}


def test_zscore_skips_single_symbol() -> None:
    assert zscore_cross_section(_panel({"a": 1.0})) == {}


def test_rank_cross_section_range() -> None:
    out = rank_cross_section(_panel({"a": 1.0, "b": 3.0, "c": 2.0}))
    assert out[D0]["a"] == pytest.approx(0.0)
    assert out[D0]["c"] == pytest.approx(0.5)
    assert out[D0]["b"] == pytest.approx(1.0)


def test_rank_skips_single_symbol() -> None:
    assert rank_cross_section(_panel({"a": 1.0})) == {}


# ==================== 合成 ====================

def test_equal_weight_is_default() -> None:
    p1 = _panel({"a": 1.0, "b": 2.0, "c": 3.0})
    p2 = _panel({"a": 3.0, "b": 2.0, "c": 1.0})
    combo = combine_panels({"x": p1, "y": p2})
    # 两个反向因子等权 → 相互抵消 → 全 0
    for v in combo[D0].values():
        assert v == pytest.approx(0.0)


def test_combine_same_direction_reinforces() -> None:
    p1 = _panel({"a": 1.0, "b": 2.0, "c": 3.0})
    p2 = _panel({"a": 1.0, "b": 2.0, "c": 3.0})
    combo = combine_panels({"x": p1, "y": p2})
    assert combo[D0]["a"] < combo[D0]["c"]
    assert combo[D0]["a"] == pytest.approx(-combo[D0]["c"])


def test_weights_are_respected() -> None:
    p1 = _panel({"a": 1.0, "b": 2.0, "c": 3.0})
    p2 = _panel({"a": 3.0, "b": 2.0, "c": 1.0})
    combo = combine_panels({"x": p1, "y": p2}, {"x": 1.0, "y": 0.0})
    # y 权重为 0 → 结果等于 x 的 z-score
    assert combo[D0]["a"] < combo[D0]["c"]


def test_missing_weight_raises() -> None:
    p1 = _panel({"a": 1.0, "b": 2.0})
    with pytest.raises(KeyError, match="weights 缺少因子"):
        combine_panels({"x": p1, "y": p1}, {"x": 1.0})


def test_unknown_method_raises() -> None:
    with pytest.raises(ValueError, match="未知 method"):
        combine_panels({"x": _panel({"a": 1.0, "b": 2.0})}, method="magic")


def test_rank_method() -> None:
    p1 = _panel({"a": 1.0, "b": 2.0, "c": 3.0})
    p2 = _panel({"a": 1.0, "b": 2.0, "c": 3.0})
    combo = combine_ranks({"x": p1, "y": p2})
    assert combo[D0]["a"] == pytest.approx(0.0)
    assert combo[D0]["c"] == pytest.approx(1.0)


def test_empty_panels() -> None:
    assert combine_panels({}) == {}


def test_scale_invariance_is_the_point() -> None:
    """**合成前必须标准化**：否则量纲大的因子会主导。

    x 量纲 0.001，y 量纲 1000；两者方向相反。若直接相加，y 完胜。
    """
    x = _panel({"a": 0.001, "b": 0.002, "c": 0.003})
    y = _panel({"a": 3000.0, "b": 2000.0, "c": 1000.0})
    combo = combine_panels({"x": x, "y": y})
    # 标准化后两者等权 → 抵消 → 全 0
    for v in combo[D0].values():
        assert abs(v) < 1e-9, "未做横截面标准化 → 量纲大的因子主导"


# ==================== 缺失处理 ====================

def test_partial_missing_uses_available_factors() -> None:
    """某标的只有部分因子有值 → 只用有值的因子，不丢整个标的。

    用 4 个标的，避免某个恰好落在中位数（z-score = 0）而误判为"被丢弃"。
    """
    p1 = {D0: {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}}
    p2 = {D0: {"a": 1.0, "c": 3.0, "d": 4.0}}  # b 缺失
    combo = combine_panels({"x": p1, "y": p2})
    assert set(combo[D0]) == {"a", "b", "c", "d"}, "缺失某因子的标的不能被丢掉"
    assert combo[D0]["b"] != 0.0, "b 仍应由 x 提供信息"


def test_all_missing_symbol_is_dropped() -> None:
    p1 = {D0: {"a": 1.0, "b": 2.0, "c": 3.0}}
    p2 = {D0: {"a": 1.0, "b": 2.0, "c": 3.0}}
    combo = combine_panels({"x": p1, "y": p2})
    assert "d" not in combo[D0]


def test_symbol_present_in_only_one_factor_uses_that_factor() -> None:
    """某标的只被一个因子覆盖 → 结果 = 该因子的 z-score（权重重归一化）。"""
    p1 = {D0: {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}}
    p2 = {D0: {"a": 1.0, "b": 2.0, "c": 3.0}}  # d 缺失
    combo = combine_panels({"x": p1, "y": p2})
    z1 = zscore_cross_section(p1)
    assert combo[D0]["d"] == pytest.approx(z1[D0]["d"])


def test_dates_union() -> None:
    p1 = {D0: {"a": 1.0, "b": 2.0}}
    p2 = {D1: {"a": 1.0, "b": 2.0}}
    combo = combine_panels({"x": p1, "y": p2})
    assert set(combo) == {D0, D1}


def test_single_symbol_date_dropped() -> None:
    """只有 1 个标的的日期无横截面信息 → 丢弃。"""
    p1 = {D0: {"a": 1.0}}
    p2 = {D0: {"a": 1.0}}
    assert combine_panels({"x": p1, "y": p2}) == {}


# ==================== 因子相关性 ====================

def test_panel_correlation_identical_is_one() -> None:
    p = {D0 + timedelta(days=i): {"a": float(i), "b": float(i + 1), "c": float(i + 2)}
         for i in range(5)}
    assert panel_correlation(p, p) == pytest.approx(1.0)


def test_panel_correlation_reversed_is_minus_one() -> None:
    a = {D0 + timedelta(days=i): {"x": 1.0, "y": 2.0, "z": 3.0} for i in range(5)}
    b = {D0 + timedelta(days=i): {"x": 3.0, "y": 2.0, "z": 1.0} for i in range(5)}
    assert panel_correlation(a, b) == pytest.approx(-1.0)


def test_panel_correlation_no_overlap_is_nan() -> None:
    a = {D0: {"x": 1.0, "y": 2.0, "z": 3.0}}
    b = {D1: {"x": 1.0, "y": 2.0, "z": 3.0}}
    assert math.isnan(panel_correlation(a, b))
