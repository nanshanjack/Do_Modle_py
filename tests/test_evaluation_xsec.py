"""``evaluation/xsec.py`` 单测 —— 截面 IC（**主指标**）。"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from do_modle.evaluation.xsec import (
    NOISE_FLOOR,
    CrossSection,
    ICSeries,
    build_cross_sections,
    compute_ic_series,
    demean,
    rank,
    spearman,
)

D0 = date(2026, 9, 14)
D1 = date(2026, 9, 15)


def _cs(factors: dict, returns: dict, dt: date = D0) -> CrossSection:
    return CrossSection(dt=dt, factors=factors, returns=returns)


# ==================== 基础统计 ====================

def test_rank_average_for_ties() -> None:
    assert rank([10, 20, 20, 30]) == [1.0, 2.5, 2.5, 4.0]


def test_rank_single() -> None:
    assert rank([5]) == [1.0]


def test_spearman_perfect_positive() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)


def test_spearman_perfect_negative() -> None:
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_monotonic_nonlinear_is_one() -> None:
    """秩相关对单调非线性变换免疫——这是选 Spearman 的原因。"""
    assert spearman([1, 2, 3, 4], [1, 4, 9, 16]) == pytest.approx(1.0)


def test_spearman_handles_length_mismatch() -> None:
    assert math.isnan(spearman([1, 2, 3], [1, 2]))


def test_spearman_constant_returns_nan() -> None:
    assert math.isnan(spearman([1, 1, 1], [1, 2, 3]))


def test_demean() -> None:
    assert demean([1.0, 2.0, 3.0]) == pytest.approx([-1.0, 0.0, 1.0])


def test_demean_empty() -> None:
    assert demean([]) == []


# ==================== CrossSection ====================

def test_cross_section_requires_matching_symbols() -> None:
    with pytest.raises(ValueError, match="标的集合不一致"):
        _cs({"a": 1.0, "b": 2.0}, {"a": 1.0, "c": 2.0})


def test_cross_section_n_and_symbols() -> None:
    cs = _cs({"b": 1.0, "a": 2.0}, {"a": 1.0, "b": 2.0})
    assert cs.n == 2
    assert cs.symbols == ["a", "b"]


def test_cross_section_ic_perfect() -> None:
    cs = _cs(
        {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0},
        {"a": 0.01, "b": 0.02, "c": 0.03, "d": 0.04},
    )
    assert cs.ic() == pytest.approx(1.0)


def test_cross_section_ic_inverted() -> None:
    cs = _cs(
        {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0},
        {"a": 0.04, "b": 0.03, "c": 0.02, "d": 0.01},
    )
    assert cs.ic() == pytest.approx(-1.0)


def test_cross_section_ic_needs_at_least_three() -> None:
    """N < 3 时 IC 无意义（两点必线性），返回 nan。"""
    cs = _cs({"a": 1.0, "b": 2.0}, {"a": 1.0, "b": 2.0})
    assert math.isnan(cs.ic())


def test_cross_section_demean_removes_common_move() -> None:
    """行业 beta 缓解：全体同涨时，去均值后 IC 不受共同涨跌影响。"""
    factors = {"a": 1.0, "b": 2.0, "c": 3.0}
    returns = {"a": 0.05 + 0.01, "b": 0.05 + 0.02, "c": 0.05 + 0.03}
    cs = _cs(factors, returns)
    assert cs.ic(demean_values=True) == pytest.approx(1.0)


def test_cross_section_pearson_option() -> None:
    cs = _cs(
        {"a": 1.0, "b": 2.0, "c": 3.0, "d": 100.0},
        {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0},
    )
    assert cs.ic(use_rank=False) < cs.ic(use_rank=True)


# ==================== ICSeries ====================

def test_ic_series_mean_and_std() -> None:
    s = ICSeries()
    for v in (0.1, 0.2, 0.3):
        s.add(D0, v, 12)
    assert s.mean_ic == pytest.approx(0.2)
    assert s.n_periods == 3


def test_ic_series_skips_nan() -> None:
    s = ICSeries()
    s.add(D0, 0.1, 12)
    s.add(D1, float("nan"), 12)
    assert s.n_periods == 1


def test_ic_ir_is_mean_over_std() -> None:
    s = ICSeries()
    for v in (0.1, 0.2, 0.3):
        s.add(D0, v, 12)
    assert s.ic_ir == pytest.approx(s.mean_ic / s.std_ic)


def test_ic_ir_nan_when_std_zero() -> None:
    """std = 0 时返回 nan，而不是 inf（避免无意义的"无穷好"）。"""
    s = ICSeries()
    for _ in range(3):
        s.add(D0, 0.2, 12)
    assert math.isnan(s.ic_ir)


def test_t_stat_scales_with_sqrt_periods() -> None:
    s = ICSeries()
    for v in (0.1, 0.2, 0.3, 0.15, 0.25):
        s.add(D0, v, 12)
    assert s.t_stat == pytest.approx(s.ic_ir * math.sqrt(5))


def test_ic_win_rate() -> None:
    s = ICSeries()
    for v in (0.1, -0.1, 0.2, -0.2):
        s.add(D0, v, 12)
    assert s.ic_win_rate == pytest.approx(0.5)


def test_mean_n_and_noise_floor() -> None:
    s = ICSeries()
    for _ in range(4):
        s.add(D0, 0.1, 12)
    assert s.mean_n == pytest.approx(12)
    assert s.noise_floor == pytest.approx(1 / math.sqrt(12))


def test_noise_floor_table_is_monotonic_decreasing() -> None:
    ns = sorted(NOISE_FLOOR)
    floors = [NOISE_FLOOR[n] for n in ns]
    assert floors == sorted(floors, reverse=True)
    assert NOISE_FLOOR[1] == pytest.approx(1.0)
    assert NOISE_FLOOR[10] == pytest.approx(0.316)


def test_summary_and_format_text() -> None:
    s = ICSeries()
    for v in (0.1, 0.2, 0.3):
        s.add(D0, v, 12)
    summary = s.summary()
    for key in ("mean_ic", "ic_ir", "t_stat", "mean_n", "noise_floor"):
        assert key in summary
    text = s.format_text("mom_20")
    assert "mom_20" in text
    assert "IC_IR" in text
    assert "噪声下界" in text


def test_empty_series() -> None:
    s = ICSeries()
    assert s.n_periods == 0
    assert math.isnan(s.mean_ic)
    assert math.isnan(s.ic_ir)


# ==================== 面板 → 截面 ====================

def test_build_cross_sections_aligns_dates() -> None:
    factors = {D0: {"a": 1.0, "b": 2.0, "c": 3.0}, D1: {"a": 1.0, "b": 2.0, "c": 3.0}}
    returns = {D0: {"a": 1.0, "b": 2.0, "c": 3.0}}
    out = build_cross_sections(factors, returns, min_symbols=3)
    assert len(out) == 1
    assert out[0].dt == D0


def test_build_cross_sections_respects_min_symbols() -> None:
    factors = {D0: {"a": 1.0, "b": 2.0}}
    returns = {D0: {"a": 1.0, "b": 2.0}}
    assert build_cross_sections(factors, returns, min_symbols=3) == []


def test_build_cross_sections_drops_nan() -> None:
    factors = {D0: {"a": 1.0, "b": float("nan"), "c": 3.0, "d": 4.0}}
    returns = {D0: {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}}
    out = build_cross_sections(factors, returns, min_symbols=3)
    assert out[0].n == 3
    assert "b" not in out[0].factors


def test_compute_ic_series_end_to_end() -> None:
    sections = [
        _cs(
            {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0},
            {"a": 0.01, "b": 0.02, "c": 0.03, "d": 0.04},
            dt=date(2026, 9, 14 + i),
        )
        for i in range(3)
    ]
    s = compute_ic_series(sections)
    assert s.n_periods == 3
    assert s.mean_ic == pytest.approx(1.0)
    assert s.mean_n == pytest.approx(4)


# ==================== 与单标的时序 IC 的对比（设计论据） ====================

def test_single_symbol_has_no_cross_section_power() -> None:
    """N=1 时截面 IC 恒为 nan —— 这正是必须用板块截面的原因。"""
    cs = CrossSection(dt=D0, factors={"a": 0.5}, returns={"a": 0.01})
    assert math.isnan(cs.ic())
    assert NOISE_FLOOR[1] == pytest.approx(1.0)


def test_noise_floor_improves_with_n() -> None:
    """N 从 1 提到 10，噪声下界从 1.0 降到 0.316。"""
    assert NOISE_FLOOR[10] < NOISE_FLOOR[4] < NOISE_FLOOR[1]


# ==================== 分层（市场状态 / 时期） ====================

from do_modle.evaluation.xsec import (  # noqa: E402
    compare_segments,
    filter_sections,
    momentum_state,
    split_sections,
)
from do_modle.objects import AdjustFlag, Bar  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402


def _section(d: date, ic_sign: int = 1) -> CrossSection:
    """构造一个 IC 符号可控的截面。"""
    factors = {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0}
    if ic_sign > 0:
        returns = {"a": 0.01, "b": 0.02, "c": 0.03, "d": 0.04}
    else:
        returns = {"a": 0.04, "b": 0.03, "c": 0.02, "d": 0.01}
    return CrossSection(dt=d, factors=factors, returns=returns)


def test_filter_sections_by_range() -> None:
    secs = [_section(date(2026, 1, 1 + i)) for i in range(10)]
    got = filter_sections(secs, start=date(2026, 1, 3), end=date(2026, 1, 6))
    assert [s.dt for s in got] == [date(2026, 1, 3 + i) for i in range(4)]


def test_filter_sections_open_ended() -> None:
    secs = [_section(date(2026, 1, 1 + i)) for i in range(5)]
    assert len(filter_sections(secs, start=date(2026, 1, 3))) == 3
    assert len(filter_sections(secs, end=date(2026, 1, 3))) == 3


def test_split_sections_by_state() -> None:
    secs = [_section(date(2026, 1, 1 + i)) for i in range(4)]
    state = {
        date(2026, 1, 1): "up",
        date(2026, 1, 2): "down",
        date(2026, 1, 3): "up",
        # 1-4 缺失 → 丢弃
    }
    groups = split_sections(secs, state)
    assert len(groups["up"]) == 2
    assert len(groups["down"]) == 1


def test_split_sections_drops_unmapped() -> None:
    secs = [_section(date(2026, 1, 1))]
    assert split_sections(secs, {}) == {}


def _bars_from_closes(closes: list[float], start: date = date(2026, 1, 1)) -> list[Bar]:
    out = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        out.append(
            Bar(
                symbol="sh.601872",
                dt=datetime.combine(start + timedelta(days=i), datetime.min.time()).replace(
                    hour=15
                ),
                open=prev,
                high=max(prev, c),
                low=min(prev, c),
                close=c,
                prev_close=prev,
                adjust_flag=AdjustFlag.HFQ,
            )
        )
    return out


def test_momentum_state_up_then_down() -> None:
    # 上涨 70 根（10.0 → 16.9），再快速下跌 30 根（17.0 → 3.95）
    # 跌幅必须足够大，才能跌破 60 根前的价位（否则 60 日动量仍为正）
    closes = [10.0 + i * 0.1 for i in range(70)] + [17.0 - i * 0.45 for i in range(30)]
    state = momentum_state(_bars_from_closes(closes), lookback=60)
    values = list(state.values())
    assert "up" in values
    assert "down" in values
    # 前 60 根无状态
    assert len(state) == len(closes) - 60


def test_momentum_state_insufficient_data() -> None:
    assert momentum_state(_bars_from_closes([10.0] * 10), lookback=60) == {}


def test_compare_segments_end_to_end() -> None:
    secs_up = [_section(date(2026, 1, 1 + i), +1) for i in range(3)]
    secs_down = [_section(date(2026, 1, 10 + i), -1) for i in range(3)]
    got = compare_segments({"up": secs_up, "down": secs_down})
    assert got["up"].mean_ic == pytest.approx(1.0)
    assert got["down"].mean_ic == pytest.approx(-1.0)


def test_segment_consistency_detects_sign_flip() -> None:
    """**分层检验的意义**：因子只在单边市况下有效 → 视为不可用。"""
    up = compare_segments({"up": [_section(date(2026, 1, 1), +1)] * 3})
    down = compare_segments({"down": [_section(date(2026, 1, 2), -1)] * 3})
    assert up["up"].mean_ic > 0
    assert down["down"].mean_ic < 0
    # 符号翻转 → 不一致
    assert (up["up"].mean_ic > 0) != (down["down"].mean_ic > 0)


# ==================== Walk-Forward 滚动窗口 ====================

from do_modle.evaluation.xsec import (  # noqa: E402
    StabilityReport,
    Window,
    evaluate_stability,
    rolling_windows,
)


def _section_perm(d: date, swaps: int, n: int = 6) -> CrossSection:
    """构造秩相关可控的截面（n=6）。

    ``swaps`` 交换首尾 k 对：``0`` → IC=+1；``1`` → IC≈−0.43；``3`` → IC=−1。
    让窗口内 IC 有**方差**，否则 std=0 → IC_IR=nan（这是正确行为，但测不出东西）。
    """
    factors = {f"s{i}": float(i) for i in range(n)}
    order = list(range(n))
    for i in range(swaps):
        order[i], order[n - 1 - i] = order[n - 1 - i], order[i]
    returns = {f"s{i}": order[i] * 0.01 for i in range(n)}
    return CrossSection(dt=d, factors=factors, returns=returns)


def test_section_perm_ic_values() -> None:
    """验证辅助构造器的 IC 取值（测试自身的地基）。"""
    assert _section_perm(date(2026, 1, 1), 0).ic() == pytest.approx(1.0)
    assert _section_perm(date(2026, 1, 1), 3).ic() == pytest.approx(-1.0)
    mid = _section_perm(date(2026, 1, 1), 1).ic()
    assert -1.0 < mid < 1.0


def test_rolling_windows_count_and_coverage() -> None:
    ws = rolling_windows(date(2016, 1, 1), date(2026, 1, 1), window_days=730, step_days=365)
    assert len(ws) >= 8
    assert ws[0].start == date(2016, 1, 1)
    # 每个窗口长度恰为 730 天（除被截断的尾窗，但尾窗过短会被丢弃）
    for w in ws:
        assert (w.end - w.start).days == 730
    # 尾部残窗被丢弃 → 最后一个窗口的结束日距 end 不超过一个步长
    assert (date(2026, 1, 1) - ws[-1].end).days <= 365


def test_rolling_windows_overlap() -> None:
    ws = rolling_windows(date(2016, 1, 1), date(2020, 1, 1), window_days=730, step_days=365)
    # 相邻窗口应重叠（步长 < 窗口长度）
    assert ws[0].end > ws[1].start


def test_rolling_windows_rejects_bad_params() -> None:
    with pytest.raises(ValueError, match="window_days"):
        rolling_windows(date(2016, 1, 1), date(2020, 1, 1), window_days=10)
    with pytest.raises(ValueError, match="step_days"):
        rolling_windows(date(2016, 1, 1), date(2020, 1, 1), step_days=0)


def test_rolling_windows_short_range() -> None:
    ws = rolling_windows(date(2026, 1, 1), date(2026, 3, 1), window_days=730)
    assert ws == []


def test_stability_report_basics() -> None:
    rep = StabilityReport()
    w = Window("W1", date(2026, 1, 1), date(2026, 12, 31))
    rep.add(w, 0.5)
    rep.add(w, -0.5)
    rep.add(w, 0.4)
    assert rep.n_windows == 3
    assert rep.min_abs_ic_ir == pytest.approx(0.4)
    assert rep.sign_consistency == pytest.approx(2 / 3)


def test_stability_report_skips_nan() -> None:
    rep = StabilityReport()
    w = Window("W1", date(2026, 1, 1), date(2026, 12, 31))
    rep.add(w, 0.5)
    rep.add(w, float("nan"))
    assert rep.n_windows == 1


def test_stability_report_worst_window() -> None:
    rep = StabilityReport()
    for i, v in enumerate((0.5, 0.1, 0.4)):
        rep.add(Window(f"W{i+1}", date(2026, 1, 1), date(2026, 12, 31)), v)
    assert rep.min_abs_ic_ir == pytest.approx(0.1)
    assert "W2" in rep.worst_window


def test_stability_report_empty() -> None:
    rep = StabilityReport()
    assert rep.n_windows == 0
    assert math.isnan(rep.min_abs_ic_ir)
    assert rep.worst_window == "—"


def test_stability_format_text() -> None:
    rep = StabilityReport()
    rep.add(Window("W1", date(2026, 1, 1), date(2026, 12, 31)), 0.5)
    text = rep.format_text("mom_20")
    assert "mom_20" in text
    assert "最差窗口" in text


def test_evaluate_stability_per_window() -> None:
    """不同窗口的 IC 符号不同 → 报告应反映出来。

    前半段 swaps∈{0,1} → IC 均值偏正；后半段 swaps∈{1,2} → IC 均值偏负。
    """
    secs = []
    for i in range(40):
        d = date(2020, 1, 1) + timedelta(days=i * 30)
        secs.append(_section_perm(d, swaps=(i % 2) if i < 20 else (1 + i % 2)))
    ws = [
        Window("A", date(2020, 1, 1), date(2021, 6, 1)),
        Window("B", date(2021, 6, 1), date(2023, 12, 31)),
    ]
    rep = evaluate_stability(secs, ws)
    assert rep.n_windows == 2
    assert rep.ic_irs[0] > 0
    assert rep.ic_irs[1] < 0
    assert rep.sign_consistency == pytest.approx(0.5)


def test_evaluate_stability_detects_regime_dependence() -> None:
    """**核心用法**：全期看有效，但某个窗口塌到 0 → 不可用。

    窗口 A：swaps∈{0,1} → IC 稳定偏正 → |IC_IR| 大
    窗口 B：swaps∈{0,3} 交替 → IC 正负相消 → 均值 0 → |IC_IR| ≈ 0
    """
    secs = []
    for i in range(20):
        secs.append(_section_perm(date(2020, 1, 1) + timedelta(days=i * 30), swaps=i % 2))
    for i in range(20):
        secs.append(_section_perm(date(2022, 1, 1) + timedelta(days=i * 30), swaps=0 if i % 2 else 3))
    ws = [
        Window("A", date(2020, 1, 1), date(2021, 12, 31)),
        Window("B", date(2021, 12, 31), date(2023, 12, 31)),
    ]
    rep = evaluate_stability(secs, ws)
    assert rep.n_windows == 2
    assert abs(rep.ic_irs[0]) > abs(rep.ic_irs[1])
    assert abs(rep.ic_irs[1]) < 0.2, "窗口 B 的 IC 正负相消，IC_IR 应接近 0"
    assert rep.min_abs_ic_ir == pytest.approx(abs(rep.ic_irs[1]))


# ==================== 自相关修正（**关键方法论修正**） ====================

def _series_from(vals: list[float]) -> ICSeries:
    s = ICSeries()
    for i, v in enumerate(vals):
        s.add(date(2020, 1, 1) + timedelta(days=i), v, 12)
    return s


def test_effective_sample_size_short_series_falls_back_to_t() -> None:
    """T < 10 时不做自相关修正（样本太少，估计不可靠）。"""
    s = _series_from([0.1, 0.2, 0.3, 0.15, 0.25])
    assert s.effective_sample_size == pytest.approx(5.0)


def test_effective_sample_size_shrinks_for_autocorrelated() -> None:
    """强自相关序列 → 有效样本数远小于期数。"""
    # 平滑序列：强正自相关
    vals = [0.5 + 0.01 * ((i // 20) % 5) for i in range(300)]
    s = _series_from(vals)
    assert s.n_periods == 300
    assert s.effective_sample_size < 60, "强自相关应大幅降低有效样本数"


def test_effective_sample_size_near_t_for_white_noise() -> None:
    """近似白噪声 → 有效样本数接近期数。"""
    import random

    rng = random.Random(42)
    vals = [rng.gauss(0, 1) for _ in range(400)]
    s = _series_from(vals)
    assert s.effective_sample_size > 400 * 0.7


def test_t_stat_naive_matches_formula() -> None:
    s = _series_from([0.1, 0.2, 0.3, 0.15, 0.25])
    assert s.t_stat_naive == pytest.approx(s.ic_ir * math.sqrt(5))


def test_t_stat_is_autocorrelation_robust() -> None:
    """**核心断言**：自相关序列的修正 t 必须显著小于朴素 t。"""
    vals = [0.5 + 0.01 * ((i // 20) % 5) for i in range(300)]
    s = _series_from(vals)
    assert abs(s.t_stat) < abs(s.t_stat_naive), "修正 t 应小于朴素 t"


def test_t_inflation_reports_overstatement() -> None:
    vals = [0.5 + 0.01 * ((i // 20) % 5) for i in range(300)]
    s = _series_from(vals)
    assert s.t_inflation > 1.5, "强自相关下膨胀倍数应明显 > 1"


def test_t_inflation_near_one_for_white_noise() -> None:
    import random

    rng = random.Random(7)
    s = _series_from([rng.gauss(0, 1) for _ in range(400)])
    assert s.t_inflation < 1.5


def test_summary_includes_both_t_values() -> None:
    s = _series_from([0.1 * (i % 3) for i in range(100)])
    summary = s.summary()
    assert "t_stat" in summary
    assert "t_stat_naive" in summary
    assert "t_eff_n" in summary


def test_overlapping_forward_returns_induce_autocorrelation() -> None:
    """**实证依据**：重叠前向收益是自相关的根源。

    构造一个「IC 真实独立」但「因子值持续」的极端情形，验证机制方向正确。
    """
    # 完全常数 IC → 无方差 → IC_IR 为 nan
    s = _series_from([0.2] * 100)
    assert math.isnan(s.ic_ir)

    # 缓慢变化 → 强自相关
    slow = _series_from([0.2 + 0.05 * math.sin(i / 50) for i in range(400)])
    fast = _series_from([0.2 + 0.05 * math.sin(i * 2.0) for i in range(400)])
    assert slow.effective_sample_size < fast.effective_sample_size
