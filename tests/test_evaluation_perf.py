"""``evaluation/perf.py`` 单测 —— 原生绩效指标（不依赖 quantstats）。"""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from do_modle.evaluation.perf import TRADING_DAYS_PER_YEAR, PerfReport, nav_from_result


def _series(values: list[float], start: date = date(2026, 1, 1)) -> list[tuple[date, float]]:
    return [(start + timedelta(days=i), v) for i, v in enumerate(values)]


def _annual_series(total_return: float, n: int = TRADING_DAYS_PER_YEAR) -> list[float]:
    """构造恰好 ``n`` 个周期、总收益为 ``total_return`` 的净值序列。"""
    return [100.0 * (1 + total_return) ** (i / n) for i in range(n + 1)]


# ==================== 构造校验 ====================

def test_requires_at_least_two_points() -> None:
    with pytest.raises(ValueError, match="至少需要 2 个点"):
        PerfReport(_series([100.0]))


def test_rejects_non_positive_nav() -> None:
    with pytest.raises(ValueError, match="净值必须为正数"):
        PerfReport(_series([100.0, 0.0]))


def test_benchmark_length_must_match() -> None:
    with pytest.raises(ValueError, match="基准长度"):
        PerfReport(_series([100.0, 101.0]), benchmark=_series([100.0, 101.0, 102.0]))


# ==================== 基础序列 ====================

def test_returns_series_length() -> None:
    r = PerfReport(_series([100.0, 110.0, 121.0]))
    assert len(r.returns) == 2
    assert r.returns[0] == pytest.approx(0.10)
    assert r.returns[1] == pytest.approx(0.10)


def test_n_periods_and_dates() -> None:
    r = PerfReport(_series([100.0, 101.0, 102.0]))
    assert r.n_periods == 2
    assert len(r.dates) == 3


# ==================== 绝对指标 ====================

def test_total_return() -> None:
    assert PerfReport(_series([100.0, 150.0])).metrics()["total_return"] == pytest.approx(0.5)


def test_annualized_return_over_exactly_one_year() -> None:
    r = PerfReport(_series(_annual_series(0.10)))
    m = r.metrics()
    assert m["years"] == pytest.approx(1.0)
    assert m["annual_return"] == pytest.approx(0.10, abs=1e-9)


def test_flat_nav_has_zero_risk_metrics() -> None:
    m = PerfReport(_series([100.0] * 50)).metrics()
    assert m["total_return"] == pytest.approx(0.0)
    assert m["annual_volatility"] == pytest.approx(0.0)
    assert m["sharpe"] == pytest.approx(0.0)
    assert m["max_drawdown"] == pytest.approx(0.0)
    assert m["win_rate"] == pytest.approx(0.0)


def test_volatility_matches_manual_computation() -> None:
    r = PerfReport(_series([100.0, 102.0, 101.0, 103.0, 105.0]))
    rets = r.returns
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    assert r.metrics()["annual_volatility"] == pytest.approx(
        math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR)
    )


def test_max_drawdown() -> None:
    m = PerfReport(_series([100.0, 120.0, 90.0, 95.0, 130.0])).metrics()
    assert m["max_drawdown"] == pytest.approx(90.0 / 120.0 - 1.0)


def test_max_drawdown_days() -> None:
    """回撤从创新高日算到最后一个仍低于该高点的交易日。"""
    m = PerfReport(_series([100.0, 120.0, 90.0, 95.0, 130.0])).metrics()
    assert m["max_drawdown_days"] == pytest.approx(2.0)


def test_win_rate() -> None:
    m = PerfReport(_series([100.0, 110.0, 100.0, 110.0, 100.0])).metrics()
    # returns: +10%, -9.09%, +10%, -9.09% -> 2 胜 2 负
    assert m["win_rate"] == pytest.approx(0.5)


def test_best_and_worst_day() -> None:
    m = PerfReport(_series([100.0, 110.0, 99.0])).metrics()
    assert m["best_day"] == pytest.approx(0.10)
    assert m["worst_day"] == pytest.approx(99.0 / 110.0 - 1.0)


def test_positive_sharpe_and_sortino_for_upward_series() -> None:
    m = PerfReport(_series([100.0 * 1.0004**i for i in range(200)])).metrics()
    assert m["sharpe"] > 0
    assert m["sortino"] > 0


def test_sortino_is_inf_when_no_negative_returns() -> None:
    """单调上升序列没有下行风险 → Sortino = inf，而不是误导性的 0.0。"""
    m = PerfReport(_series([100.0 * 1.0004**i for i in range(200)])).metrics()
    assert math.isinf(m["sortino"])


def test_sortino_finite_when_downside_exists() -> None:
    m = PerfReport(_series([100.0, 110.0, 100.0, 108.0, 99.0])).metrics()
    assert math.isfinite(m["sortino"])


def test_calmar_uses_annual_return_over_max_dd() -> None:
    m = PerfReport(_series(_annual_series(0.10))).metrics()
    if m["max_drawdown"] < 0:
        assert m["calmar"] == pytest.approx(m["annual_return"] / abs(m["max_drawdown"]))


# ==================== 相对基准 ====================

def test_relative_requires_benchmark() -> None:
    """不允许只报绝对收益——基准缺失必须报错。"""
    with pytest.raises(ValueError, match="未提供基准"):
        PerfReport(_series([100.0, 101.0])).relative()


def test_excess_total_return() -> None:
    nav = _series([100.0, 102.0, 104.0, 103.0, 105.0])
    bench = _series([100.0, 101.0, 102.0, 103.0, 104.0])
    rel = PerfReport(nav, benchmark=bench).relative()
    assert rel["benchmark_total_return"] == pytest.approx(0.04)
    assert rel["excess_total_return"] == pytest.approx(0.01)


def test_beta_of_identical_series_is_one() -> None:
    nav = _series([100.0, 102.0, 101.0, 105.0])
    rel = PerfReport(nav, benchmark=list(nav)).relative()
    assert rel["beta"] == pytest.approx(1.0)
    assert rel["tracking_error"] == pytest.approx(0.0)
    assert rel["excess_total_return"] == pytest.approx(0.0)


def test_alpha_zero_when_identical_to_benchmark() -> None:
    nav = _series(_annual_series(0.10))
    rel = PerfReport(nav, benchmark=list(nav)).relative()
    assert rel["alpha_annual"] == pytest.approx(0.0, abs=1e-9)


def test_t_stat_zero_when_no_excess() -> None:
    nav = _series([100.0, 102.0, 101.0, 105.0])
    rel = PerfReport(nav, benchmark=list(nav)).relative()
    assert rel["t_stat_excess"] == pytest.approx(0.0)


def test_t_stat_positive_when_outperforming() -> None:
    nav = _series([100.0 * 1.001**i for i in range(250)])
    bench = _series([100.0 * 1.0002**i for i in range(250)])
    rel = PerfReport(nav, benchmark=bench).relative()
    assert rel["t_stat_excess"] > 0
    assert rel["information_ratio"] > 0


# ==================== 汇总与输出 ====================

def test_summary_merges_absolute_and_relative() -> None:
    nav = _series([100.0, 102.0, 104.0])
    s = PerfReport(nav, benchmark=list(nav)).summary()
    assert "total_return" in s
    assert "excess_total_return" in s


def test_summary_without_benchmark_has_no_relative_keys() -> None:
    s = PerfReport(_series([100.0, 102.0])).summary()
    assert "total_return" in s
    assert "excess_total_return" not in s


def test_format_text_contains_key_lines() -> None:
    nav = _series([100.0, 102.0, 104.0])
    text = PerfReport(nav, benchmark=list(nav)).format_text()
    for key in ("区间", "总收益", "Sharpe", "最大回撤", "超额总收益", "信息比率"):
        assert key in text


def test_html_without_quantstats_raises_clear_error(tmp_path, monkeypatch) -> None:
    """未安装 quantstats 时给出可操作的报错，不静默降级。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name in ("quantstats", "pandas"):
            raise ImportError("simulated missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="quantstats"):
        PerfReport(_series([100.0, 101.0])).html(tmp_path / "r.html")


# ==================== 与 BacktestResult 衔接 ====================

def test_nav_from_result() -> None:
    class FakeResult:
        nav = [(date(2026, 1, 1), 100.0), (date(2026, 1, 2), 101.0)]

    assert nav_from_result(FakeResult()) == FakeResult.nav


def test_periods_per_year_configurable() -> None:
    r = PerfReport(_series([100.0, 110.0]), periods_per_year=12)
    assert r.metrics()["years"] == pytest.approx(1 / 12)
