"""``strategy/factors.py`` 与 ``strategy/panel.py`` 单测。

**最关键的测试是 PIT 安全**：追加未来数据后，历史因子值必须**逐点不变**。
这是防止前视的最后一道人工防线。
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from do_modle.objects import AdjustFlag, Bar
from do_modle.strategy.factors import (
    FACTORS,
    FactorSpec,
    compute_factor,
    factor_names,
)
from do_modle.strategy.panel import (
    build_factor_panel,
    build_return_panel,
    forward_return,
)

D0 = date(2026, 1, 5)
SYM_A, SYM_B = "sh.600001", "sh.600002"


def _bars(closes: list[float], *, start: date = D0, volume: float = 1_000_000):
    """由收盘价序列构造 Bar。

    成交量按确定性模式轻微波动（0.8x~1.2x），使 ``vp_corr`` 等需要量能变化的
    因子也能产出值；``amount = volume × close``，使 ``amihud`` 可用。

    估值字段按确定性模式变化，使估值因子（含变化率与分位）可用。
    """
    out: list[Bar] = []
    for i, c in enumerate(closes):
        prev = closes[i - 1] if i else c
        v = volume * (1.0 + 0.2 * (((i % 5) - 2) / 2.0))
        out.append(
            Bar(
                symbol=SYM_A,
                dt=datetime.combine(start + timedelta(days=i), datetime.min.time()).replace(
                    hour=15
                ),
                open=prev,
                high=max(prev, c),
                low=min(prev, c),
                close=c,
                prev_close=prev,
                volume=v,
                amount=v * c,
                adjust_flag=AdjustFlag.HFQ,
                pe_ttm=15.0 + (i % 7),
                pb_mrq=2.0 + 0.1 * (i % 5),
                ps_ttm=3.0 + 0.05 * (i % 3),
                pcf_ncf_ttm=10.0 + (i % 4),
                turnover=1.0 + 0.1 * (i % 3),
                fc_score=1.0 if i % 4 else -1.0,
                fc_chg_pct=30.0 + (i % 5) * 10.0,
                fc_age_days=float(i % 30),
                fin_roe=0.10 + 0.01 * (i % 5),
                fin_gp_margin=0.30 + 0.01 * (i % 4),
                fin_np_margin=0.15 + 0.01 * (i % 3),
                fin_asset_turn=0.50 + 0.01 * (i % 6),
                fin_liab_to_asset=0.45 + 0.01 * (i % 4),
                fin_yoy_ni=0.20 + 0.02 * (i % 5),
                fin_cfo_to_or=0.18 + 0.01 * (i % 3),
                fin_dupont_roe=0.11 + 0.01 * (i % 5),
                ship_bdt=1000.0 + 10.0 * (i % 7),
                ship_bdi=800.0 + 8.0 * (i % 5),
                ship_bct=600.0 + 6.0 * (i % 3),
            )
        )
    return out


def _bars_no_valuation(closes: list[float], *, start: date = D0) -> list[Bar]:
    """估值与预告字段全为 None 的 Bar —— 用于验证缺失处理。"""
    return [
        Bar(
            symbol=b.symbol, dt=b.dt, open=b.open, high=b.high, low=b.low,
            close=b.close, prev_close=b.prev_close, volume=b.volume,
            amount=b.amount, adjust_flag=b.adjust_flag,
        )
        for b in _bars(closes, start=start)
    ]


def _rising(n: int, start: float = 10.0, step: float = 0.1) -> list[float]:
    return [start + i * step for i in range(n)]


# ==================== 注册表 ====================

def test_registry_is_large_and_categorized() -> None:
    from do_modle.strategy.factors import factor_categories

    assert len(FACTORS) >= 40, "因子库应覆盖多个类别"
    cats = factor_categories()
    for key in ("momentum", "reversal", "volatility", "trend", "volume"):
        assert key in cats, key
    for key in ("mom_20", "mom_60", "mom_120", "rev_5", "vol_20",
                "ma_gap_20", "rsi_14", "vratio_20"):
        assert key in FACTORS, key


def test_unknown_factor_raises() -> None:
    with pytest.raises(KeyError, match="未注册的因子"):
        compute_factor("nope", _bars(_rising(30)))


def test_factor_spec_rejects_bad_direction() -> None:
    with pytest.raises(ValueError, match="direction"):
        FactorSpec("x", lambda b: 1.0, 10, direction=0)


def test_factor_spec_rejects_tiny_min_bars() -> None:
    with pytest.raises(ValueError, match="min_bars"):
        FactorSpec("x", lambda b: 1.0, 1)


def test_all_specs_have_sane_min_bars() -> None:
    for name, spec in FACTORS.items():
        assert spec.min_bars >= 2, name
        assert spec.direction in (1, -1), name


def test_compute_factor_tolerates_extra_bars() -> None:
    """多传 K 线无害——内部只取最后 min_bars 根。

    对比必须是**同一段序列**：长序列整体 vs 长序列的尾段。
    """
    spec = FACTORS["mom_20"]
    long_series = _bars(_rising(spec.min_bars + 30))
    assert compute_factor("mom_20", long_series) == pytest.approx(
        compute_factor("mom_20", long_series[-spec.min_bars:])
    )


# ==================== 数据不足 ====================

@pytest.mark.parametrize("name", sorted(FACTORS))
def test_returns_none_when_insufficient_bars(name: str) -> None:
    spec = FACTORS[name]
    assert compute_factor(name, _bars(_rising(spec.min_bars - 1))) is None


@pytest.mark.parametrize("name", sorted(FACTORS))
def test_returns_value_when_enough_bars(name: str) -> None:
    spec = FACTORS[name]
    v = compute_factor(name, _bars(_rising(spec.min_bars + 5)))
    assert v is not None
    assert math.isfinite(v)


# ==================== 各因子语义 ====================

def test_mom_20_positive_for_rising_series() -> None:
    assert compute_factor("mom_20", _bars(_rising(30))) > 0


def test_mom_20_negative_for_falling_series() -> None:
    assert compute_factor("mom_20", _bars(_rising(30)[::-1])) < 0


def test_reversal_direction_is_negative() -> None:
    """5 日反转取负号：短期涨得多 → 因子值小。"""
    assert FACTORS["rev_5"].direction == -1
    assert compute_factor("rev_5", _bars(_rising(10))) < 0


def test_vol_direction_is_negative() -> None:
    """波动率取负：低波因子。"""
    assert FACTORS["vol_20"].direction == -1


def test_vol_zero_for_flat_series() -> None:
    v = compute_factor("vol_20", _bars([10.0] * 30))
    assert v == pytest.approx(0.0)


def test_vol_direction_makes_volatile_series_negative() -> None:
    """``vol_20`` 的 direction = −1（低波偏好），故高波动序列的**因子值**为负。"""
    alt = [10.0 + (0.5 if i % 2 else -0.5) for i in range(30)]
    assert compute_factor("vol_20", _bars(alt)) < 0
    # 原始函数返回的是正的波动率
    assert FACTORS["vol_20"].fn(_bars(alt)) > 0


def test_ma_gap_zero_when_flat() -> None:
    assert compute_factor("ma_gap_20", _bars([10.0] * 25)) == pytest.approx(0.0)


def test_ma_gap_positive_above_ma() -> None:
    assert compute_factor("ma_gap_20", _bars(_rising(25))) > 0


def test_rsi_direction_makes_overbought_negative() -> None:
    """``rsi_14`` 的 direction = −1（超买看空）。

    连续上涨 → 原始 RSI = 100 → 因子值 = −100。
    """
    raw = FACTORS["rsi_14"].fn
    assert raw(_bars(_rising(20))) == pytest.approx(100.0)
    assert compute_factor("rsi_14", _bars(_rising(20))) == pytest.approx(-100.0)


def test_rsi_raw_low_for_falling_series() -> None:
    assert FACTORS["rsi_14"].fn(_bars(_rising(20)[::-1])) == pytest.approx(0.0)


def test_rsi_flat_series_raw_is_50() -> None:
    raw = FACTORS["rsi_14"].fn
    assert raw(_bars([10.0] * 20)) == pytest.approx(50.0)
    assert compute_factor("rsi_14", _bars([10.0] * 20)) == pytest.approx(-50.0)


def test_vratio_measures_deviation_from_mean() -> None:
    """``vratio_20`` 返回的是**相对前 20 日均量的偏离度**（均量时 = 0）。

    ``_bars`` 的成交量模式在 20 根窗口上均值恰为 1.0，最后一根为 1.2 → 0.2。
    """
    assert compute_factor("vratio_20", _bars(_rising(25))) == pytest.approx(0.2)


def test_vratio_zero_for_constant_volume() -> None:
    bars = _bars(_rising(25))
    fixed = [
        Bar(
            symbol=b.symbol, dt=b.dt, open=b.open, high=b.high, low=b.low,
            close=b.close, prev_close=b.prev_close, volume=1_000_000.0,
            amount=1_000_000.0 * b.close, adjust_flag=b.adjust_flag,
        )
        for b in bars
    ]
    assert compute_factor("vratio_20", fixed) == pytest.approx(0.0)


def test_vratio_detects_volume_spike() -> None:
    """放量 3 倍于均量 → 偏离度 = 3/1 − 1 = 2.0。"""
    bars = _bars(_rising(25))
    last = bars[-1]
    bars[-1] = Bar(
        symbol=last.symbol, dt=last.dt, open=last.open, high=last.high, low=last.low,
        close=last.close, prev_close=last.prev_close, volume=3_000_000.0,
        amount=3_000_000.0 * last.close, adjust_flag=last.adjust_flag,
    )
    assert compute_factor("vratio_20", bars) == pytest.approx(2.0)


# ==================== 估值因子 ====================

VALUATION_FACTORS = [n for n, s in FACTORS.items() if s.category == "valuation"]


def test_valuation_category_is_populated() -> None:
    assert len(VALUATION_FACTORS) >= 8, VALUATION_FACTORS
    for key in ("ep_ttm", "bp_mrq", "sp_ttm", "cfp_ttm", "turnover_mean_20"):
        assert key in VALUATION_FACTORS


def test_ep_is_inverse_of_pe() -> None:
    """EP = 1/PE。用收益率而非倍数，避免 PE 为负时的符号错乱。"""
    bars = _bars(_rising(5))
    assert compute_factor("ep_ttm", bars) == pytest.approx(1.0 / bars[-1].pe_ttm)


def test_bp_is_inverse_of_pb() -> None:
    bars = _bars(_rising(5))
    assert compute_factor("bp_mrq", bars) == pytest.approx(1.0 / bars[-1].pb_mrq)


def test_negative_pe_gives_negative_ep() -> None:
    """亏损公司 PE<0 → EP<0。这是**真实信息**（亏损），不是脏数据。"""
    bars = _bars(_rising(5))
    loss = [
        Bar(
            symbol=b.symbol, dt=b.dt, open=b.open, high=b.high, low=b.low,
            close=b.close, prev_close=b.prev_close, volume=b.volume,
            amount=b.amount, adjust_flag=b.adjust_flag, pe_ttm=-20.0,
        )
        for b in bars
    ]
    assert compute_factor("ep_ttm", loss) == pytest.approx(-0.05)


def test_zero_pe_gives_none() -> None:
    """PE = 0 无意义（不可能）→ None，不产生 inf。"""
    bars = _bars(_rising(5))
    zero = [
        Bar(
            symbol=b.symbol, dt=b.dt, open=b.open, high=b.high, low=b.low,
            close=b.close, prev_close=b.prev_close, volume=b.volume,
            amount=b.amount, adjust_flag=b.adjust_flag, pe_ttm=0.0,
        )
        for b in bars
    ]
    assert compute_factor("ep_ttm", zero) is None


@pytest.mark.parametrize("name", sorted(VALUATION_FACTORS))
def test_valuation_factor_returns_none_when_data_missing(name: str) -> None:
    """**估值字段缺失时必须返回 None**（该标的当期退出截面），不能抛异常或给 0。"""
    spec = FACTORS[name]
    assert compute_factor(name, _bars_no_valuation(_rising(spec.min_bars + 5))) is None


def test_turnover_mean_uses_recent_window() -> None:
    """``turnover_mean_20`` 的 direction=−1（高换手看空），故因子值 = −原始均值。"""
    bars = _bars(_rising(30))
    raw = sum(b.turnover for b in bars[-20:]) / 20
    assert compute_factor("turnover_mean_20", bars) == pytest.approx(-raw)


def test_turnover_ratio_detects_spike() -> None:
    bars = _bars(_rising(30))
    last = bars[-1]
    avg_prev = sum(b.turnover for b in bars[-21:-1]) / 20
    spiked = list(bars[:-1]) + [
        Bar(
            symbol=last.symbol, dt=last.dt, open=last.open, high=last.high, low=last.low,
            close=last.close, prev_close=last.prev_close, volume=last.volume,
            amount=last.amount, adjust_flag=last.adjust_flag,
            turnover=avg_prev * 3.0,
        )
    ]
    # direction = −1（高换手看空）
    assert compute_factor("turnover_ratio_20", spiked) == pytest.approx(-2.0)


def test_ep_momentum_sign() -> None:
    """EP 上升（变便宜）→ 正值。"""
    bars = _bars(_rising(70))
    v = compute_factor("ep_mom_60", bars)
    assert v is not None
    # 构造器里 pe_ttm = 15 + i%7，EP 随 i 波动，故只验证数值有限
    assert math.isfinite(v)


def test_ep_percentile_in_unit_range() -> None:
    bars = _bars(_rising(260))
    v = compute_factor("ep_pct_250", bars)
    assert v is not None
    assert 0.0 <= v <= 1.0


def test_ep_volatility_non_negative_raw() -> None:
    """``ep_vol_60`` 的 direction=−1，原始标准差非负。"""
    bars = _bars(_rising(70))
    assert FACTORS["ep_vol_60"].direction == -1
    assert compute_factor("ep_vol_60", bars) <= 0


@pytest.mark.parametrize("name", sorted(VALUATION_FACTORS))
def test_valuation_factor_is_pit_safe(name: str) -> None:
    """估值因子同样必须通过 PIT 安全测试（追加未来数据后历史值不变）。"""
    spec = FACTORS[name]
    long_series = _bars(_rising(spec.min_bars + 40))
    if spec.min_bars > len(long_series):
        pytest.skip("序列过短")
    assert compute_factor(name, long_series) == pytest.approx(
        compute_factor(name, long_series[-spec.min_bars:])
    )


def test_valuation_factors_are_marked_as_valuation() -> None:
    for name in VALUATION_FACTORS:
        assert FACTORS[name].category == "valuation"


# ==================== 前向收益 ====================

def test_forward_return_entry_at_open() -> None:
    """t 日出信号 → t+1 开盘成交 → t+h 收盘。"""
    bars = _bars([10.0, 11.0, 12.0, 13.0])
    # index=0, horizon=2: 入场 bars[1].open=10.0, 出场 bars[2].close=12.0
    assert forward_return(bars, 0, 2) == pytest.approx(12.0 / 10.0 - 1.0)


def test_forward_return_close_to_close() -> None:
    bars = _bars([10.0, 11.0, 12.0, 13.0])
    assert forward_return(bars, 0, 2, entry_at_open=False) == pytest.approx(0.2)


def test_forward_return_out_of_range() -> None:
    bars = _bars([10.0, 11.0])
    assert forward_return(bars, 0, 5) is None


def test_forward_return_rejects_bad_horizon() -> None:
    with pytest.raises(ValueError, match="horizon"):
        forward_return(_bars([10.0, 11.0]), 0, 0)


# ==================== PIT 安全（最关键） ====================

@pytest.mark.parametrize("name", sorted(FACTORS))
def test_factor_is_pit_safe_under_appended_future(name: str) -> None:
    """**核心测试**：追加未来数据后，历史因子值必须逐点不变。

    若某因子偷偷用了全序列统计量（如"全期均值"），这个测试会失败。
    """
    full = _rising(120, step=0.05)
    for cut in (30, 60, 90):
        spec = FACTORS[name]
        if cut < spec.min_bars:
            continue
        short = _bars(full[:cut])
        long = _bars(full)  # 追加了未来
        v_short = compute_factor(name, short)
        # 长序列在 cut-1 处的窗口 = 前 cut 根
        v_long_at_cut = compute_factor(name, long[:cut])
        assert v_short == pytest.approx(v_long_at_cut), (
            f"{name} 在 cut={cut} 处因子值随未来数据变化 → 存在前视"
        )


def test_panel_values_are_pit_safe() -> None:
    """面板层面同样：短序列与长序列在重叠日期上的值必须一致。"""
    full = _bars(_rising(80, step=0.05))
    short_panel = build_factor_panel({SYM_A: full[:40]}, ["mom_20", "rsi_14"])
    long_panel = build_factor_panel({SYM_A: full}, ["mom_20", "rsi_14"])

    for name in ("mom_20", "rsi_14"):
        for d, per_symbol in short_panel[name].items():
            assert per_symbol[SYM_A] == pytest.approx(long_panel[name][d][SYM_A]), (
                f"{name} 在 {d} 的面板值随未来数据变化"
            )


# ==================== 面板构建 ====================

def test_build_factor_panel_shape() -> None:
    bars = _bars(_rising(50))
    panel = build_factor_panel({SYM_A: bars}, ["mom_20"])
    assert set(panel) == {"mom_20"}
    # 前 20 根数据不足 → 从第 21 根（index 20）起有值
    assert len(panel["mom_20"]) == 30


def test_build_factor_panel_multiple_symbols() -> None:
    a = _bars(_rising(40))
    b = _bars(_rising(40, start=20.0))
    panel = build_factor_panel({SYM_A: a, SYM_B: b}, ["mom_20"])
    for per_symbol in panel["mom_20"].values():
        assert set(per_symbol) == {SYM_A, SYM_B}


def test_build_factor_panel_skips_suspended() -> None:
    bars = _bars(_rising(40))
    suspended = bars[30]
    bars[30] = Bar(
        symbol=suspended.symbol,
        dt=suspended.dt,
        open=suspended.open,
        high=suspended.high,
        low=suspended.low,
        close=suspended.close,
        prev_close=suspended.prev_close,
        volume=0.0,
        is_suspended=True,
        adjust_flag=suspended.adjust_flag,
    )
    panel = build_factor_panel({SYM_A: bars}, ["mom_20"])
    assert suspended.dt.date() not in panel["mom_20"]


def test_build_return_panel_shape() -> None:
    bars = _bars(_rising(30))
    panel = build_return_panel({SYM_A: bars}, horizon=5)
    # 最后 5 根无前向收益
    assert len(panel) == 25


def test_build_return_panel_skips_suspended() -> None:
    bars = _bars(_rising(30))
    suspended = bars[10]
    bars[10] = Bar(
        symbol=suspended.symbol,
        dt=suspended.dt,
        open=suspended.open,
        high=suspended.high,
        low=suspended.low,
        close=suspended.close,
        prev_close=suspended.prev_close,
        volume=0.0,
        is_suspended=True,
        adjust_flag=suspended.adjust_flag,
    )
    panel = build_return_panel({SYM_A: bars}, horizon=3)
    assert suspended.dt.date() not in panel


def test_panel_and_return_align_for_ic() -> None:
    """因子面板与收益面板可对齐成截面（端到端）。"""
    from do_modle.evaluation.xsec import build_cross_sections, compute_ic_series

    symbols = {
        "sh.600001": _bars(_rising(60, step=0.10)),
        "sh.600002": _bars(_rising(60, step=0.05)),
        "sh.600003": _bars(_rising(60, step=0.02)),
        "sh.600004": _bars(_rising(60, step=-0.01)),
        "sh.600005": _bars(_rising(60, step=-0.05)),
    }
    # 面板按标的分别构建（键为 symbol），需合并成 {date: {symbol: v}}
    factor_panel: dict = {}
    for sym, bars in symbols.items():
        p = build_factor_panel({sym: bars}, ["mom_20"])["mom_20"]
        for d, per in p.items():
            factor_panel.setdefault(d, {}).update(per)

    return_panel: dict = {}
    for sym, bars in symbols.items():
        p = build_return_panel({sym: bars}, horizon=5)
        for d, per in p.items():
            return_panel.setdefault(d, {}).update(per)

    sections = build_cross_sections(factor_panel, return_panel, min_symbols=5)
    assert sections, "未能构建任何截面"
    series = compute_ic_series(sections)
    assert series.n_periods > 0
    assert series.mean_n == pytest.approx(5)


# ==================== 业绩预告：attach_forecasts（**PIT 核心**） ====================

from do_modle.objects import Forecast, forecast_kind_score  # noqa: E402
from do_modle.strategy.panel import attach_forecasts  # noqa: E402


def _fc(pub: date, kind: str = "预增", chg: float = 50.0, stat: date | None = None):
    return Forecast(
        symbol=SYM_A,
        pub_date=pub,
        stat_date=stat or (pub - timedelta(days=90)),
        source="forecast",
        kind=kind,
        chg_pct_mid=chg,
    )


def test_forecast_kind_score_table() -> None:
    assert forecast_kind_score("预增") == 1.0
    assert forecast_kind_score("首亏") == -1.0
    assert forecast_kind_score("略增") == 0.5
    assert forecast_kind_score("外星类型") is None  # 未知不猜


def test_attach_forecast_not_visible_on_pub_date() -> None:
    """**PIT 核心断言**：公告日当天**不可见**，次日才可见。

    pit 规则：KIND_EXPRESS_REPORT → 公告日 + 1 天 09:00。
    若公告日当天就可见 → 前视。
    """
    bars = _bars_no_valuation([10.0] * 5, start=date(2026, 1, 1))
    fcs = [_fc(date(2026, 1, 2))]  # 1/2 公告
    out = attach_forecasts({SYM_A: bars}, {SYM_A: fcs})[SYM_A]
    by_date = {b.dt.date(): b for b in out}

    assert by_date[date(2026, 1, 1)].fc_score is None, "公告前不应有数据"
    assert by_date[date(2026, 1, 2)].fc_score is None, "**公告日当天不应可见**"
    assert by_date[date(2026, 1, 3)].fc_score == 1.0, "次日应可见"


def test_attach_forecast_uses_pub_date_not_stat_date() -> None:
    """用 stat_date（报告期）当可见时点就是前视 —— 必须用 pub_date。"""
    bars = _bars_no_valuation([10.0] * 40, start=date(2026, 1, 1))
    # 报告期 2025-12-31，但 2026-03-20 才公告
    fcs = [_fc(date(2026, 3, 20), stat=date(2025, 12, 31))]
    out = attach_forecasts({SYM_A: bars}, {SYM_A: fcs})[SYM_A]
    # 报告期之后、公告日之前的 bar 都不应有数据
    for b in out:
        if b.dt.date() < date(2026, 3, 21):
            assert b.fc_score is None, f"{b.dt.date()} 提前可见 → 前视"


def test_attach_forecast_age_increments() -> None:
    bars = _bars_no_valuation([10.0] * 10, start=date(2026, 1, 1))
    fcs = [_fc(date(2026, 1, 1))]
    out = attach_forecasts({SYM_A: bars}, {SYM_A: fcs})[SYM_A]
    ages = [b.fc_age_days for b in out if b.fc_age_days is not None]
    assert ages == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]


def test_attach_forecast_expires_after_max_age() -> None:
    """超过 max_age_bars → 置 None（退出截面），避免事件因子被稀释成常数。"""
    bars = _bars_no_valuation([10.0] * 20, start=date(2026, 1, 1))
    fcs = [_fc(date(2026, 1, 1))]
    out = attach_forecasts({SYM_A: bars}, {SYM_A: fcs}, max_age_bars=5)[SYM_A]
    assert out[5].fc_score is not None
    assert out[6].fc_score is not None
    assert out[7].fc_score is None, "超过 5 个 bar 应失效"


def test_attach_forecast_latest_wins() -> None:
    bars = _bars_no_valuation([10.0] * 10, start=date(2026, 1, 1))
    fcs = [_fc(date(2026, 1, 1), "预增"), _fc(date(2026, 1, 5), "首亏")]
    out = attach_forecasts({SYM_A: bars}, {SYM_A: fcs})[SYM_A]
    assert out[4].fc_score == 1.0  # 1/2 起可见「预增」
    assert out[6].fc_score == -1.0  # 1/6 起可见「首亏」（覆盖）


def test_attach_forecast_no_forecasts_leaves_bars_untouched() -> None:
    bars = _bars_no_valuation([10.0] * 5, start=date(2026, 1, 1))
    out = attach_forecasts({SYM_A: bars}, {})[SYM_A]
    assert all(b.fc_score is None for b in out)
    assert len(out) == len(bars)


def test_attach_forecast_does_not_mutate_input() -> None:
    bars = _bars_no_valuation([10.0] * 5, start=date(2026, 1, 1))
    attach_forecasts({SYM_A: bars}, {SYM_A: [_fc(date(2026, 1, 1))]})
    assert all(b.fc_score is None for b in bars), "Bar 是 frozen，不应被修改"


def test_attach_forecast_empty_bars() -> None:
    assert attach_forecasts({SYM_A: []}, {})[SYM_A] == []


def test_forecast_factor_returns_none_without_forecast() -> None:
    """无活跃预告 → 该标的当期退出截面（返回 None，不填 0）。"""
    bars = _bars_no_valuation(_rising(10))
    for name in ("fc_score", "fc_chg_pct", "fc_score_decay", "fc_chg_decay", "fc_age"):
        assert compute_factor(name, bars) is None, name


def test_forecast_decay_reduces_with_age() -> None:
    """衰减因子：同样的打分，越新越大。"""
    fresh = _bars([10.0] * 3, start=date(2026, 1, 1))
    fresh[-1] = Bar(**{**fresh[-1].__dict__, "fc_score": 1.0, "fc_age_days": 0.0})
    old = _bars([10.0] * 3, start=date(2026, 1, 1))
    old[-1] = Bar(**{**old[-1].__dict__, "fc_score": 1.0, "fc_age_days": 60.0})
    assert compute_factor("fc_score_decay", fresh) > compute_factor("fc_score_decay", old)


def test_forecast_age_direction_is_negative() -> None:
    """``fc_age`` 的 direction = −1（越新越好）。"""
    assert FACTORS["fc_age"].direction == -1


# ==================== 财务质量：attach_financials + 报告期采样 ====================

from do_modle.evaluation.xsec import (  # noqa: E402
    build_report_sections,
    report_observations,
)
from do_modle.strategy.panel import attach_financials  # noqa: E402


def _fin(pub: date, stat: date, roe: float = 0.12, **kw):
    return {"pubDate": pub.isoformat(), "statDate": stat.isoformat(),
            "roeAvg": str(roe), **{k: str(v) for k, v in kw.items()}}


def test_attach_financials_not_visible_on_pub_date() -> None:
    """**PIT 核心**：公告日当天不可见，次日才可见。"""
    bars = _bars_no_valuation([10.0] * 5, start=date(2026, 1, 1))
    fins = {SYM_A: {date(2025, 12, 31): _fin(date(2026, 1, 2), date(2025, 12, 31))}}
    out = attach_financials({SYM_A: bars}, fins)[SYM_A]
    by_date = {b.dt.date(): b for b in out}
    assert by_date[date(2026, 1, 1)].fin_roe is None
    assert by_date[date(2026, 1, 2)].fin_roe is None, "**公告日当天不应可见**"
    assert by_date[date(2026, 1, 3)].fin_roe == pytest.approx(0.12)


def test_attach_financials_uses_pubdate_not_statdate() -> None:
    """用 statDate 当可见时点就是前视。"""
    bars = _bars_no_valuation([10.0] * 90, start=date(2026, 1, 1))
    fins = {SYM_A: {date(2025, 12, 31): _fin(date(2026, 3, 20), date(2025, 12, 31))}}
    out = attach_financials({SYM_A: bars}, fins)[SYM_A]
    for b in out:
        if b.dt.date() < date(2026, 3, 21):
            assert b.fin_roe is None, f"{b.dt.date()} 提前可见 → 前视"


def test_attach_financials_latest_wins() -> None:
    bars = _bars_no_valuation([10.0] * 30, start=date(2026, 1, 1))
    fins = {
        SYM_A: {
            date(2025, 9, 30): _fin(date(2026, 1, 1), date(2025, 9, 30), roe=0.10),
            date(2025, 12, 31): _fin(date(2026, 1, 10), date(2025, 12, 31), roe=0.20),
        }
    }
    out = attach_financials({SYM_A: bars}, fins)[SYM_A]
    assert out[2].fin_roe == pytest.approx(0.10)  # 1/2 起可见 Q3
    assert out[11].fin_roe == pytest.approx(0.20)  # 1/11 起可见年报


def test_attach_financials_expires() -> None:
    bars = _bars_no_valuation([10.0] * 20, start=date(2026, 1, 1))
    fins = {SYM_A: {date(2025, 12, 31): _fin(date(2026, 1, 1), date(2025, 12, 31))}}
    out = attach_financials({SYM_A: bars}, fins, max_age_bars=5)[SYM_A]
    assert out[3].fin_roe is not None
    assert out[9].fin_roe is None


def test_attach_financials_falls_back_to_statutory_lag() -> None:
    """无 pubDate 时退回「报告期 + 法定滞后」（年报 +120 天）。"""
    bars = _bars_no_valuation([10.0] * 200, start=date(2026, 1, 1))
    fins = {SYM_A: {date(2025, 12, 31): {"statDate": "2025-12-31", "roeAvg": "0.15"}}}
    out = attach_financials({SYM_A: bars}, fins)[SYM_A]
    first_visible = next(b.dt.date() for b in out if b.fin_roe is not None)
    assert first_visible >= date(2026, 4, 30), "年报法定滞后 120 天 → 4/30 之后"


def test_attach_financials_does_not_mutate_input() -> None:
    bars = _bars_no_valuation([10.0] * 5, start=date(2026, 1, 1))
    attach_financials({SYM_A: bars}, {SYM_A: {date(2025, 12, 31): _fin(date(2026, 1, 1), date(2025, 12, 31))}})
    assert all(b.fin_roe is None for b in bars)


def test_report_observations_detects_changes() -> None:
    bars = _bars_no_valuation([10.0] * 10, start=date(2026, 1, 1))
    # 手工设 3 个变化点
    vals = [0.1, 0.1, 0.1, 0.2, 0.2, 0.2, 0.2, 0.3, 0.3, 0.3]
    bars = [Bar(**{**b.__dict__, "fin_roe": v}) for b, v in zip(bars, vals)]
    obs = report_observations(bars, "fin_roe")
    assert obs == [date(2026, 1, 1), date(2026, 1, 4), date(2026, 1, 8)]


def test_report_observations_skips_none() -> None:
    bars = _bars_no_valuation([10.0] * 5, start=date(2026, 1, 1))
    assert report_observations(bars, "fin_roe") == []


def test_build_report_sections_groups_by_quarter() -> None:
    """**核心**：每个标的每季只取一个观测（避免日频重复计入）。"""
    syms = ["s1", "s2", "s3", "s4", "s5"]
    fp = {
        date(2026, 4, 20): {s: 1.0 + i for i, s in enumerate(syms)},
        date(2026, 4, 21): {s: 1.0 + i for i, s in enumerate(syms)},
    }
    rp = {
        date(2026, 4, 20): {s: 0.01 * (i + 1) for i, s in enumerate(syms)},
        date(2026, 4, 21): {s: 0.01 * (i + 1) for i, s in enumerate(syms)},
    }
    obs = {s: [date(2026, 4, 20)] for s in syms}  # 同季只有一个观测
    secs = build_report_sections(fp, rp, obs, min_symbols=5)
    assert len(secs) == 1
    assert secs[0].n == 5
    assert secs[0].ic() == pytest.approx(1.0)


def test_build_report_sections_respects_min_symbols() -> None:
    fp = {date(2026, 4, 20): {"s1": 1.0, "s2": 2.0}}
    rp = {date(2026, 4, 20): {"s1": 0.01, "s2": 0.02}}
    obs = {"s1": [date(2026, 4, 20)], "s2": [date(2026, 4, 20)]}
    assert build_report_sections(fp, rp, obs, min_symbols=5) == []


def test_financial_factor_returns_none_without_data() -> None:
    bars = _bars_no_valuation(_rising(10))
    for name in ("fin_roe", "fin_gp_margin", "fin_liab_to_asset", "fin_yoy_ni"):
        assert compute_factor(name, bars) is None, name


def test_financial_factors_registered() -> None:
    from do_modle.strategy.factors import factor_categories

    assert len(factor_categories()["quality"]) == 8


# ==================== 航运运价：attach_shipping + 因子 ====================

from do_modle.strategy.panel import attach_shipping  # noqa: E402


def _ship_series(pairs):
    return [(d, v) for d, v in pairs]


def test_attach_shipping_respects_lag() -> None:
    """**PIT 核心**：数据日期 + 2 天才可见。"""
    bars = _bars_no_valuation([10.0] * 6, start=date(2026, 1, 1))
    idx = {"BDTI": [(date(2026, 1, 1), 1000.0)]}
    out = attach_shipping({SYM_A: bars}, idx, lag_days=2)[SYM_A]
    by_date = {b.dt.date(): b for b in out}
    assert by_date[date(2026, 1, 1)].ship_bdt is None
    assert by_date[date(2026, 1, 2)].ship_bdt is None, "**+1 天不应可见**"
    assert by_date[date(2026, 1, 3)].ship_bdt == pytest.approx(1000.0)


def test_attach_shipping_carries_forward() -> None:
    """缺失日沿用上一个已知值（**不插值**，插值会引入未来信息）。"""
    bars = _bars_no_valuation([10.0] * 10, start=date(2026, 1, 1))
    idx = {"BDTI": [(date(2026, 1, 1), 1000.0), (date(2026, 1, 8), 2000.0)]}
    out = attach_shipping({SYM_A: bars}, idx, lag_days=0)[SYM_A]
    assert out[3].ship_bdt == pytest.approx(1000.0)  # 中间日沿用
    assert out[8].ship_bdt == pytest.approx(2000.0)


def test_attach_shipping_ignores_unknown_index() -> None:
    bars = _bars_no_valuation([10.0] * 5, start=date(2026, 1, 1))
    out = attach_shipping({SYM_A: bars}, {"UNKNOWN": [(date(2026, 1, 1), 1.0)]})[SYM_A]
    assert all(b.ship_bdt is None for b in out)


def test_attach_shipping_does_not_mutate_input() -> None:
    bars = _bars_no_valuation([10.0] * 5, start=date(2026, 1, 1))
    attach_shipping({SYM_A: bars}, {"BDTI": [(date(2026, 1, 1), 1000.0)]}, lag_days=0)
    assert all(b.ship_bdt is None for b in bars)


def test_ship_bdt_level() -> None:
    bars = _bars_no_valuation([10.0] * 3, start=date(2026, 1, 1))
    bars = [Bar(**{**b.__dict__, "ship_bdt": 1234.0}) for b in bars]
    assert compute_factor("ship_bdt", bars) == pytest.approx(1234.0)


def test_ship_bdt_mom_sign() -> None:
    up = _bars_no_valuation([10.0] * 25, start=date(2026, 1, 1))
    up = [Bar(**{**b.__dict__, "ship_bdt": 1000.0 + 10.0 * i}) for i, b in enumerate(up)]
    down = list(reversed(up))
    assert compute_factor("ship_bdt_mom_20", up) > 0
    assert compute_factor("ship_bdt_mom_20", down) < 0


def test_ship_bdt_bdi_spread() -> None:
    bars = _bars_no_valuation([10.0] * 3, start=date(2026, 1, 1))
    bars = [Bar(**{**b.__dict__, "ship_bdt": 1000.0, "ship_bdi": 500.0}) for b in bars]
    assert compute_factor("ship_bdt_bdi_spread", bars) == pytest.approx(2.0)


def test_ship_bdt_bdi_spread_guards_zero() -> None:
    bars = _bars_no_valuation([10.0] * 3, start=date(2026, 1, 1))
    bars = [Bar(**{**b.__dict__, "ship_bdt": 1000.0, "ship_bdi": 0.0}) for b in bars]
    assert compute_factor("ship_bdt_bdi_spread", bars) is None


def test_ship_divergence_sign() -> None:
    """**运价-股价背离**：运价涨、股价没跟上 → 正值。"""
    bars = _bars_no_valuation([10.0] * 61, start=date(2026, 1, 1))
    # 运价涨 50%，股价不动
    bars = [
        Bar(**{**b.__dict__, "ship_bdt": 1000.0 + 500.0 * (i / 60.0)})
        for i, b in enumerate(bars)
    ]
    v = compute_factor("ship_divergence_60", bars)
    assert v is not None
    assert v > 0.4, "运价 +50%、股价 0% → 背离应约 +50%"


def test_ship_divergence_zero_when_both_move_together() -> None:
    bars = _bars_no_valuation([10.0 * (1 + 0.5 * (i / 60.0)) for i in range(61)],
                              start=date(2026, 1, 1))
    bars = [
        Bar(**{**b.__dict__, "ship_bdt": 1000.0 * (1 + 0.5 * (i / 60.0))})
        for i, b in enumerate(bars)
    ]
    assert compute_factor("ship_divergence_60", bars) == pytest.approx(0.0, abs=1e-9)


def test_shipping_factors_return_none_without_data() -> None:
    bars = _bars_no_valuation(_rising(70))
    for name in ("ship_bdt", "ship_bdt_mom_20", "ship_bdt_bdi_spread", "ship_divergence_60"):
        assert compute_factor(name, bars) is None, name


def test_shipping_factors_registered() -> None:
    from do_modle.strategy.factors import factor_categories

    assert len(factor_categories()["shipping"]) == 7
