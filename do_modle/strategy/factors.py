"""因子库 —— 纯函数，**只使用截至 t 的数据**。

每个因子是 ``(bars) -> float | None`` 的纯函数，``bars`` **必须只含截至 t 的
K 线**（由 ``panel.py`` 通过 ``PITGuard`` 保证）。因子内部**不得**访问任何
未来数据，也不得访问全序列统计量（如"全期均值"）——那会引入前视。

**价格口径**：因子必须用**后复权价**（``AdjustFlag.HFQ``）。
前复权会重算历史价格 → 前视；不复权在除权日会产生假跳空 → 假信号。

``FactorSpec.min_bars`` 是**该因子恰好需要的 K 线数**（不是"至少"）。
``panel.py`` 据此做 O(1) 切片，避免 O(n²) 的内存拷贝。

因子方向 ``direction``：``+1`` 越大越看多，``-1`` 越小越看多。
``compute_factor`` 已乘上 direction，故 **IC 为正即代表因子有效**。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Sequence

from do_modle.objects import Bar

__all__ = [
    "FactorSpec",
    "FACTORS",
    "compute_factor",
    "factor_names",
    "factor_categories",
]

FactorFn = Callable[[Sequence[Bar]], "float | None"]


@dataclass(frozen=True)
class FactorSpec:
    name: str
    fn: FactorFn
    min_bars: int
    direction: int = 1
    category: str = "price"

    def __post_init__(self) -> None:
        if self.direction not in (1, -1):
            raise ValueError(f"direction 只能是 +1/-1，收到 {self.direction}")
        if self.min_bars < 2:
            raise ValueError(f"min_bars 必须 >= 2，收到 {self.min_bars}")


# ==================== 基础工具 ====================


def _std(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _safe(a: float, b: float) -> float | None:
    return a / b if b else None


def _rets(closes: Sequence[float]) -> list[float]:
    return [
        closes[i] / closes[i - 1] - 1.0
        for i in range(1, len(closes))
        if closes[i - 1] > 0
    ]


# ==================== 因子实现（按类别） ====================


def _momentum(bars: Sequence[Bar], lookback: int) -> float | None:
    base = bars[0].close
    return _safe(bars[-1].close - base, base)


def _volatility(bars: Sequence[Bar], lookback: int) -> float | None:
    r = _rets([b.close for b in bars])
    return _std(r) * math.sqrt(244) if r else None


def _ma_gap(bars: Sequence[Bar], lookback: int) -> float | None:
    closes = [b.close for b in bars]
    ma = _mean(closes)
    return _safe(closes[-1] - ma, ma)


def _price_position(bars: Sequence[Bar], lookback: int) -> float | None:
    """收盘价在 N 日高低区间中的位置（0=最低，1=最高）。"""
    hi = max(b.high for b in bars)
    lo = min(b.low for b in bars)
    if hi <= lo:
        return None
    return (bars[-1].close - lo) / (hi - lo)


def _rsi(bars: Sequence[Bar], lookback: int) -> float | None:
    closes = [b.close for b in bars]
    gains = losses = 0.0
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if gains + losses == 0:
        return 50.0
    if losses == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def _volume_ratio(bars: Sequence[Bar], lookback: int) -> float | None:
    """当日量相对前 N 日均量的偏离。``bars`` 长度 = lookback + 1。"""
    vols = [b.volume for b in bars]
    avg = _mean(vols[:-1])
    return _safe(vols[-1] - avg, avg)


def _volume_volatility(bars: Sequence[Bar], lookback: int) -> float | None:
    """成交量的变异系数。"""
    vols = [b.volume for b in bars]
    m = _mean(vols)
    return _safe(_std(vols), m)


def _amihud(bars: Sequence[Bar], lookback: int) -> float | None:
    """Amihud 非流动性：``mean(|ret| / 成交额)`` × 1e9。值越大越不流动。"""
    r = _rets([b.close for b in bars])
    amounts = [b.amount for b in bars[1:]]
    vals = [abs(x) / a for x, a in zip(r, amounts) if a > 0]
    return _mean(vals) * 1e9 if vals else None


def _volume_price_corr(bars: Sequence[Bar], lookback: int) -> float | None:
    """量价相关（收益率 vs 成交量变化）。"""
    closes = [b.close for b in bars]
    vols = [b.volume for b in bars]
    r = _rets(closes)
    dv = [vols[i] / vols[i - 1] - 1.0 for i in range(1, len(vols)) if vols[i - 1] > 0]
    n = min(len(r), len(dv))
    if n < 3:
        return None
    x, y = r[-n:], dv[-n:]
    mx, my = _mean(x), _mean(y)
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    dx = math.sqrt(sum((a - mx) ** 2 for a in x))
    dy = math.sqrt(sum((b - my) ** 2 for b in y))
    return _safe(num, dx * dy)


def _max_daily_return(bars: Sequence[Bar], lookback: int) -> float | None:
    """区间最大单日涨幅 —— 彩票效应（彩票型股票未来收益低）。"""
    r = _rets([b.close for b in bars])
    return max(r) if r else None


def _skewness(bars: Sequence[Bar], lookback: int) -> float | None:
    r = _rets([b.close for b in bars])
    if len(r) < 3:
        return None
    m = _mean(r)
    s = _std(r)
    if s == 0:
        return 0.0
    return sum(((x - m) / s) ** 3 for x in r) / len(r)


def _downside_ratio(bars: Sequence[Bar], lookback: int) -> float | None:
    """下行波动 / 总波动。越高 = 收益分布越偏负。"""
    r = _rets([b.close for b in bars])
    if len(r) < 3:
        return None
    total = _std(r)
    if total == 0:
        return None
    down = [x for x in r if x < 0]
    return _std(down) / total if len(down) >= 2 else 0.0


def _consecutive_up(bars: Sequence[Bar]) -> float | None:
    """最近连续上涨天数。"""
    closes = [b.close for b in bars]
    n = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            n += 1
        else:
            break
    return float(n)


def _gap_frequency(bars: Sequence[Bar], lookback: int) -> float | None:
    """跳空频率：``|open - prev_close| / prev_close > 1%`` 的占比。"""
    vals = []
    for b in bars:
        if b.prev_close > 0:
            vals.append(abs(b.open - b.prev_close) / b.prev_close)
    if not vals:
        return None
    return sum(1 for v in vals if v > 0.01) / len(vals)


def _acceleration(bars: Sequence[Bar]) -> float | None:
    """价格加速度：``mom_20 - mom_60``。需要 61 根。"""
    closes = [b.close for b in bars]
    m20 = closes[-1] / closes[-21] - 1.0
    m60 = closes[-1] / closes[-61] - 1.0
    return m20 - m60


def _high_proximity(bars: Sequence[Bar], lookback: int) -> float | None:
    """距 N 日最高价的接近度：``close / max(high) - 1``（≤0）。

    52 周高点邻近效应：接近历史高点的股票后续表现更好。
    """
    hi = max(b.high for b in bars)
    return _safe(bars[-1].close - hi, hi)


def _turnover_trend(bars: Sequence[Bar], lookback: int) -> float | None:
    """成交量的线性趋势斜率（按均值标准化）。"""
    vols = [b.volume for b in bars]
    m = _mean(vols)
    if m <= 0:
        return None
    n = len(vols)
    xs = list(range(n))
    mx = (n - 1) / 2
    num = sum((x - mx) * (v - m) for x, v in zip(xs, vols))
    den = sum((x - mx) ** 2 for x in xs)
    return _safe(num, den * m)


def _intraday_range(bars: Sequence[Bar], lookback: int) -> float | None:
    """平均日内振幅 ``(high - low) / prev_close``。"""
    vals = [
        (b.high - b.low) / b.prev_close for b in bars if b.prev_close > 0
    ]
    return _mean(vals) if vals else None


# ==================== 估值类（PIT 安全，见 Bar 文档） ====================


def _yield(pe: float | None) -> float | None:
    """把估值倍数转成收益率。``pe == 0`` 无意义 → None。

    负值保留（亏损 → EP<0；净资产为负 → BP<0），这是**真实信息**，不是脏数据。
    极端值不影响 Spearman（只用秩）。
    """
    if pe is None or pe == 0:
        return None
    return 1.0 / pe


def _ep(bars: Sequence[Bar]) -> float | None:
    """盈利收益率 ``1/peTTM``。"""
    return _yield(bars[-1].pe_ttm)


def _bp(bars: Sequence[Bar]) -> float | None:
    """账面市值比 ``1/pbMRQ``。"""
    return _yield(bars[-1].pb_mrq)


def _sp(bars: Sequence[Bar]) -> float | None:
    """营收收益率 ``1/psTTM``。"""
    return _yield(bars[-1].ps_ttm)


def _cfp(bars: Sequence[Bar]) -> float | None:
    """现金流收益率 ``1/pcfNcfTTM``。"""
    return _yield(bars[-1].pcf_ncf_ttm)


def _turnover_mean(bars: Sequence[Bar], lookback: int) -> float | None:
    """近 N 日平均换手率（%）。"""
    vals = [b.turnover for b in bars if b.turnover is not None]
    return _mean(vals) if vals else None


def _turnover_ratio(bars: Sequence[Bar], lookback: int) -> float | None:
    """当日换手率 / 前 N 日均换手率 − 1。``bars`` 长度 = lookback + 1。"""
    prev = [b.turnover for b in bars[:-1] if b.turnover is not None]
    cur = bars[-1].turnover
    if cur is None or not prev:
        return None
    avg = _mean(prev)
    return _safe(cur - avg, avg)


def _valuation_momentum(bars: Sequence[Bar], lookback: int, which: str) -> float | None:
    """估值收益率的 N 日变化：``yield_t − yield_{t-N}``。

    正值 = 变得更便宜（估值下降）。用收益率而非倍数，避免 PE 为负时的符号错乱。
    """
    get = (lambda b: _yield(b.pe_ttm)) if which == "ep" else (lambda b: _yield(b.pb_mrq))
    a, b_ = get(bars[0]), get(bars[-1])
    return None if a is None or b_ is None else b_ - a


def _valuation_volatility(bars: Sequence[Bar], lookback: int) -> float | None:
    """EP 的 N 日标准差 —— 估值不确定性。"""
    vals = [v for v in (_yield(b.pe_ttm) for b in bars) if v is not None]
    return _std(vals) if len(vals) >= 2 else None


def _valuation_percentile(bars: Sequence[Bar], lookback: int) -> float | None:
    """当前 EP 在自身 N 日历史中的分位（0~1）。

    高 = 相对自身历史便宜。这是**时序**分位，但与截面 IC 兼容：
    每个标的给出自己的"便宜程度"，再在截面上比较。
    """
    vals = [v for v in (_yield(b.pe_ttm) for b in bars) if v is not None]
    if len(vals) < 20:
        return None
    cur = vals[-1]
    return sum(1 for v in vals if v <= cur) / len(vals)


# ==================== 业绩预告类（PIT 安全，见 panel.attach_forecasts） ====================
#
# 这三个字段由 ``panel.attach_forecasts`` 填充，**不是数据源直出**：
#   fc_score    预告类型打分（预增 +1 … 首亏 −1）
#   fc_chg_pct  净利润同比变动中值（%）
#   fc_age_days 距公告日的**交易日**间隔
#
# 无活跃预告（或已超过 max_age_bars）时全为 None → 该标的当期退出截面。


def _fc_score(bars: Sequence[Bar]) -> float | None:
    return bars[-1].fc_score


def _fc_chg(bars: Sequence[Bar]) -> float | None:
    return bars[-1].fc_chg_pct


def _fc_age(bars: Sequence[Bar]) -> float | None:
    return bars[-1].fc_age_days


def _decay(age: float | None, half_life: float = 20.0) -> float | None:
    """时间衰减权重：``1 / (1 + age / half_life)``。"""
    if age is None:
        return None
    return 1.0 / (1.0 + max(age, 0.0) / half_life)


def _fc_score_decay(bars: Sequence[Bar]) -> float | None:
    """预告类型打分 × 时间衰减。越新的预告权重越高。"""
    bar = bars[-1]
    w = _decay(bar.fc_age_days)
    return None if bar.fc_score is None or w is None else bar.fc_score * w


def _fc_chg_decay(bars: Sequence[Bar]) -> float | None:
    """预告变动幅度 × 时间衰减。"""
    bar = bars[-1]
    w = _decay(bar.fc_age_days)
    return None if bar.fc_chg_pct is None or w is None else bar.fc_chg_pct * w


# ==================== 财务质量类（PIT 安全，见 panel.attach_financials） ====================
#
# 字段由 ``panel.attach_financials`` 填充，**不是数据源直出**。
# 财报是**季度**数据 → 因子值在两次公告之间完全不变。
# **评估必须按报告期采样**（``xsec.build_report_sections``），不能用日频 IC。


def _fin_roe(bars: Sequence[Bar]) -> float | None:
    """ROE（平均）。质量因子核心。"""
    return bars[-1].fin_roe


def _fin_gp_margin(bars: Sequence[Bar]) -> float | None:
    """毛利率。"""
    return bars[-1].fin_gp_margin


def _fin_np_margin(bars: Sequence[Bar]) -> float | None:
    """净利率。"""
    return bars[-1].fin_np_margin


def _fin_asset_turn(bars: Sequence[Bar]) -> float | None:
    """总资产周转率。"""
    return bars[-1].fin_asset_turn


def _fin_liab_to_asset(bars: Sequence[Bar]) -> float | None:
    """资产负债率。"""
    return bars[-1].fin_liab_to_asset


def _fin_yoy_ni(bars: Sequence[Bar]) -> float | None:
    """净利润同比增速。"""
    return bars[-1].fin_yoy_ni


def _fin_cfo_to_or(bars: Sequence[Bar]) -> float | None:
    """经营现金流 / 营业收入（盈利质量）。"""
    return bars[-1].fin_cfo_to_or


def _fin_dupont_roe(bars: Sequence[Bar]) -> float | None:
    """杜邦 ROE。"""
    return bars[-1].fin_dupont_roe


# ==================== 航运运价类（PIT 安全，见 panel.attach_shipping） ====================
#
# 字段由 ``panel.attach_shipping`` 填充（数据日期 + 2 天可见）。
# **只做有经济含义的构造，不做参数搜索** —— 避免在运价上重演"换参数找最优"的过拟合。


def _ship_bdt(bars: Sequence[Bar]) -> float | None:
    """BDTI 原油运输指数（水平）。"""
    return bars[-1].ship_bdt


def _ship_bdt_mom(bars: Sequence[Bar], lookback: int) -> float | None:
    """BDTI 变化率。"""
    a, b = bars[0].ship_bdt, bars[-1].ship_bdt
    return _safe(b - a, a) if a and b is not None else None


def _ship_bdi_mom(bars: Sequence[Bar], lookback: int) -> float | None:
    """BDI 变化率。"""
    a, b = bars[0].ship_bdi, bars[-1].ship_bdi
    return _safe(b - a, a) if a and b is not None else None


def _ship_bdt_zscore(bars: Sequence[Bar], lookback: int) -> float | None:
    """BDTI 相对近 N 日均值的 z 分数（运价偏离常态的程度）。"""
    vals = [b.ship_bdt for b in bars if b.ship_bdt is not None]
    if len(vals) < 20:
        return None
    m, sd = _mean(vals), _std(vals)
    return _safe(vals[-1] - m, sd) if sd else None


def _ship_bdt_bdi_spread(bars: Sequence[Bar]) -> float | None:
    """BDTI / BDI 比值 —— 原油运输 vs 干散货的相对强弱。"""
    bdt, bdi = bars[-1].ship_bdt, bars[-1].ship_bdi
    if bdt is None or bdi is None or bdi == 0:
        return None
    return bdt / bdi


def _ship_price_divergence(bars: Sequence[Bar], lookback: int) -> float | None:
    """**运价-股价背离**：``运价变化率 − 股价变化率``。

    正值 = 运价已涨但股价未跟上 → 看多（基本面领先股价）。
    这是运价数据**最有经济含义**的用法：不是运价本身，而是它与股价的**背离**。
    """
    if len(bars) < lookback + 1:
        return None
    a_ship, b_ship = bars[0].ship_bdt, bars[-1].ship_bdt
    a_px, b_px = bars[0].close, bars[-1].close
    if a_ship is None or b_ship is None or a_ship == 0 or a_px <= 0:
        return None
    ship_ret = b_ship / a_ship - 1.0
    px_ret = b_px / a_px - 1.0
    return ship_ret - px_ret


# ==================== 注册表（参数化生成） ====================


def _specs() -> list[FactorSpec]:
    out: list[FactorSpec] = []

    # 动量（正）—— 覆盖短中长期
    for L in (5, 10, 20, 40, 60, 90, 120, 180, 250):
        out.append(
            FactorSpec(f"mom_{L}", lambda b, L=L: _momentum(b, L), L + 1, +1, "momentum")
        )

    # 短期反转（负）
    for L in (1, 3, 5, 10, 20):
        out.append(
            FactorSpec(f"rev_{L}", lambda b, L=L: _momentum(b, L), L + 1, -1, "reversal")
        )

    # 波动（负）
    for L in (10, 20, 40, 60, 120):
        out.append(
            FactorSpec(f"vol_{L}", lambda b, L=L: _volatility(b, L), L + 1, -1, "volatility")
        )
    out.append(
        FactorSpec("downside_ratio_60", lambda b: _downside_ratio(b, 60), 61, -1, "volatility")
    )

    # 趋势（正）
    for L in (5, 10, 20, 60, 120):
        out.append(FactorSpec(f"ma_gap_{L}", lambda b, L=L: _ma_gap(b, L), L, +1, "trend"))
    for L in (20, 60, 120, 250):
        out.append(
            FactorSpec(
                f"price_pos_{L}", lambda b, L=L: _price_position(b, L), L, +1, "trend"
            )
        )

    # 超买超卖（负）
    for L in (6, 14, 24):
        out.append(FactorSpec(f"rsi_{L}", lambda b, L=L: _rsi(b, L), L + 1, -1, "overbought"))

    # 量能 / 流动性
    for L in (5, 20, 60):
        out.append(
            FactorSpec(f"vratio_{L}", lambda b, L=L: _volume_ratio(b, L), L + 1, +1, "volume")
        )
    out.append(FactorSpec("vol_of_vol_20", lambda b: _volume_volatility(b, 20), 20, -1, "volume"))
    out.append(FactorSpec("amihud_20", lambda b: _amihud(b, 20), 21, +1, "liquidity"))
    out.append(FactorSpec("vp_corr_20", lambda b: _volume_price_corr(b, 20), 21, +1, "volume"))
    out.append(
        FactorSpec("turnover_trend_20", lambda b: _turnover_trend(b, 20), 20, +1, "volume")
    )

    # 形态 / 极值 / 高阶矩
    for L in (20, 60):
        out.append(
            FactorSpec(
                f"max_ret_{L}", lambda b, L=L: _max_daily_return(b, L), L + 1, -1, "lottery"
            )
        )
    out.append(FactorSpec("skew_60", lambda b: _skewness(b, 60), 61, -1, "higher_moment"))
    out.append(FactorSpec("consec_up", _consecutive_up, 21, -1, "pattern"))
    out.append(FactorSpec("gap_freq_20", lambda b: _gap_frequency(b, 20), 21, -1, "pattern"))
    out.append(FactorSpec("accel_20_60", _acceleration, 61, +1, "momentum"))
    out.append(FactorSpec("high_prox_250", lambda b: _high_proximity(b, 250), 250, +1, "trend"))
    out.append(
        FactorSpec("intraday_range_20", lambda b: _intraday_range(b, 20), 20, -1, "volatility")
    )

    # ---- 估值类（PIT 安全；用收益率而非倍数，避免负值符号错乱）----
    # 方向先验：价值溢价 → 高收益率（便宜）未来收益更高 → direction=+1
    # 注意：搜索用 |IC_IR| 判定，方向不影响是否通过三道闸。
    out.append(FactorSpec("ep_ttm", _ep, 2, +1, "valuation"))
    out.append(FactorSpec("bp_mrq", _bp, 2, +1, "valuation"))
    out.append(FactorSpec("sp_ttm", _sp, 2, +1, "valuation"))
    out.append(FactorSpec("cfp_ttm", _cfp, 2, +1, "valuation"))
    for L in (20, 60):
        out.append(
            FactorSpec(
                f"turnover_mean_{L}",
                lambda b, L=L: _turnover_mean(b, L),
                L,
                -1,  # 高换手 = 高关注 → 未来收益低
                "valuation",
            )
        )
    out.append(
        FactorSpec(
            "turnover_ratio_20", lambda b: _turnover_ratio(b, 20), 21, -1, "valuation"
        )
    )
    out.append(
        FactorSpec(
            "ep_mom_60", lambda b: _valuation_momentum(b, 60, "ep"), 61, +1, "valuation"
        )
    )
    out.append(
        FactorSpec(
            "bp_mom_60", lambda b: _valuation_momentum(b, 60, "bp"), 61, +1, "valuation"
        )
    )
    out.append(
        FactorSpec("ep_vol_60", lambda b: _valuation_volatility(b, 60), 61, -1, "valuation")
    )
    out.append(
        FactorSpec(
            "ep_pct_250", lambda b: _valuation_percentile(b, 250), 250, +1, "valuation"
        )
    )

    # ---- 业绩预告类（事件驱动；字段由 panel.attach_forecasts 填充）----
    out.append(FactorSpec("fc_score", _fc_score, 2, +1, "forecast"))
    out.append(FactorSpec("fc_chg_pct", _fc_chg, 2, +1, "forecast"))
    out.append(FactorSpec("fc_score_decay", _fc_score_decay, 2, +1, "forecast"))
    out.append(FactorSpec("fc_chg_decay", _fc_chg_decay, 2, +1, "forecast"))
    out.append(FactorSpec("fc_age", _fc_age, 2, -1, "forecast"))

    # ---- 财务质量类（季度数据；**必须按报告期采样评估**）----
    out.append(FactorSpec("fin_roe", _fin_roe, 2, +1, "quality"))
    out.append(FactorSpec("fin_gp_margin", _fin_gp_margin, 2, +1, "quality"))
    out.append(FactorSpec("fin_np_margin", _fin_np_margin, 2, +1, "quality"))
    out.append(FactorSpec("fin_asset_turn", _fin_asset_turn, 2, +1, "quality"))
    out.append(FactorSpec("fin_liab_to_asset", _fin_liab_to_asset, 2, -1, "quality"))
    out.append(FactorSpec("fin_yoy_ni", _fin_yoy_ni, 2, +1, "quality"))
    out.append(FactorSpec("fin_cfo_to_or", _fin_cfo_to_or, 2, +1, "quality"))
    out.append(FactorSpec("fin_dupont_roe", _fin_dupont_roe, 2, +1, "quality"))

    # ---- 航运运价类（**只做有经济含义的构造**）----
    out.append(FactorSpec("ship_bdt", _ship_bdt, 2, +1, "shipping"))
    out.append(FactorSpec("ship_bdt_mom_20", lambda b: _ship_bdt_mom(b, 20), 21, +1, "shipping"))
    out.append(FactorSpec("ship_bdt_mom_60", lambda b: _ship_bdt_mom(b, 60), 61, +1, "shipping"))
    out.append(FactorSpec("ship_bdi_mom_20", lambda b: _ship_bdi_mom(b, 20), 21, +1, "shipping"))
    out.append(
        FactorSpec("ship_bdt_zscore_60", lambda b: _ship_bdt_zscore(b, 60), 60, +1, "shipping")
    )
    out.append(FactorSpec("ship_bdt_bdi_spread", _ship_bdt_bdi_spread, 2, +1, "shipping"))
    out.append(
        FactorSpec(
            "ship_divergence_60", lambda b: _ship_price_divergence(b, 60), 61, +1, "shipping"
        )
    )

    return out


FACTORS: dict[str, FactorSpec] = {s.name: s for s in _specs()}


def factor_names() -> list[str]:
    return sorted(FACTORS)


def factor_categories() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, spec in FACTORS.items():
        out.setdefault(spec.category, []).append(name)
    return {k: sorted(v) for k, v in out.items()}


def compute_factor(name: str, bars: Sequence[Bar]) -> float | None:
    """计算因子值；数据不足时返回 ``None``。

    返回值已乘 ``direction``，故 **IC 为正即代表因子有效**。

    内部只取最后 ``spec.min_bars`` 根，故调用方可传入整段历史而不损失正确性，
    但 ``panel.py`` 会按需精确切片以避免 O(n²) 拷贝。
    """
    spec = FACTORS.get(name)
    if spec is None:
        raise KeyError(f"未注册的因子: {name!r}；可用 {len(FACTORS)} 个")
    if len(bars) < spec.min_bars:
        return None
    window = bars[-spec.min_bars :] if len(bars) > spec.min_bars else bars
    v = spec.fn(window)
    if v is None or v != v:
        return None
    return v * spec.direction
