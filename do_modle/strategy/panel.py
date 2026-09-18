"""从缓存构建**因子面板**与**收益面板** —— 截面 IC 的输入。

面板结构：``{日期: {标的: 数值}}``。

**PIT 安全**：

* 因子值在 t 日收盘后计算，只使用 **≤ t** 的 K 线
* 收益为 **t+1 开盘 → t+1+h 收盘**（与撮合口径一致，避免用 t 日收盘成交 t 日信号）
* 因子用**后复权价**（历史不变，无前视）

**停牌处理**：停牌日不产生因子值也不产生收益（该标的当期缺席截面）。
"""

from __future__ import annotations

from datetime import date
from typing import Mapping, Sequence

from dataclasses import replace
from datetime import timedelta
from typing import Any

from do_modle.data.pit import FINANCIAL_LAG_DAYS, KIND_EXPRESS_REPORT, PITGuard
from do_modle.objects import AdjustFlag, Bar, Forecast, forecast_kind_score
from do_modle.strategy.factors import compute_factor

__all__ = [
    "Panel",
    "attach_financials",
    "attach_shipping",
    "attach_forecasts",
    "build_factor_panel",
    "build_return_panel",
    "forward_return",
]

Panel = dict[date, dict[str, float]]


def forward_return(
    bars: Sequence[Bar],
    index: int,
    horizon: int,
    *,
    entry_at_open: bool = True,
) -> float | None:
    """从 ``bars[index]`` 起算的前向收益。

    ``entry_at_open=True``（默认）：``close_{i+h} / open_{i+1} - 1``
    —— 与撮合口径一致（T 日出信号、T+1 开盘成交）。

    ``entry_at_open=False``：``close_{i+h} / close_i - 1``（学术常用口径）。

    区间内若有停牌日（Bar 缺失），调用方应自行保证 ``bars`` 是连续可交易序列。
    """
    if horizon < 1:
        raise ValueError(f"horizon 必须 >= 1，收到 {horizon}")
    end = index + horizon
    if end >= len(bars):
        return None
    if entry_at_open:
        entry = bars[index + 1].open
        exit_ = bars[end].close
    else:
        entry = bars[index].close
        exit_ = bars[end].close
    if entry <= 0:
        return None
    return exit_ / entry - 1.0


def build_factor_panel(
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    factor_names: Sequence[str],
    *,
    min_history: int = 1,
    pit: PITGuard | None = None,
    assert_no_lookahead: bool = True,
) -> dict[str, Panel]:
    """构建 ``{因子名: {日期: {标的: 因子值}}}``。

    **性能**：按 ``FactorSpec.min_bars`` **精确切片**（而非 ``bars[:i+1]``），
    避免 O(n²) 的列表拷贝。因子只依赖最近 ``min_bars`` 根，故结果等价。
    """
    from do_modle.strategy.factors import FACTORS

    guard = pit or PITGuard()
    out: dict[str, Panel] = {name: {} for name in factor_names}
    windows = {name: FACTORS[name].min_bars for name in factor_names}

    for symbol, bars in bars_by_symbol.items():
        if not bars:
            continue
        ordered = sorted(bars, key=lambda b: b.dt)
        if assert_no_lookahead:
            # 每根 Bar 必须在它自己的收盘时刻之后才可见
            guard.assert_no_lookahead_bars(ordered, ordered[-1].dt)

        for i, bar in enumerate(ordered):
            if i < min_history - 1 or bar.is_suspended:
                continue
            d = bar.dt.date()
            for name in factor_names:
                w = windows[name]
                if i + 1 < w:
                    continue
                # 只含 ≤ t 的最近 w 根
                v = compute_factor(name, ordered[i + 1 - w : i + 1])
                if v is not None:
                    out[name].setdefault(d, {})[symbol] = v
    return out


def attach_forecasts(
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    forecasts_by_symbol: Mapping[str, Sequence[Forecast]],
    *,
    pit: PITGuard | None = None,
    max_age_bars: int = 120,
) -> dict[str, list[Bar]]:
    """把「**截至该 Bar 当日已公告**」的最近一次业绩预告附加到 Bar 上。

    ## PIT 规则（关键）

    ``data.pit.KIND_EXPRESS_REPORT`` 定义：**公告日次日 09:00** 可见。
    Bar 是 15:00 收盘，故公告日次日及以后的 Bar 才能看到该预告。
    用 ``stat_date``（报告期）当可见时点就是**前视**——会提前知道未公布的业绩。

    ## 信息衰减

    ``max_age_bars`` 之后置 ``None``（退出截面）。理由：业绩预告是**事件驱动**信息，
    公告 120 个交易日之后，那条预告对当期截面排序基本无解释力。
    若不设上限，会让"最近一次预告"永远有效，把事件因子稀释成常数。

    返回新的 Bar 列表（不修改输入，``Bar`` 是 frozen）。
    """
    guard = pit or PITGuard()
    out: dict[str, list[Bar]] = {}

    for symbol, bars in bars_by_symbol.items():
        ordered = sorted(bars, key=lambda b: b.dt)
        if not ordered:
            out[symbol] = []
            continue

        # 每条预告的「可见日」= 公告日次日（按 pit 规则算出后再取日期）
        # 注意：只按日期排序。日期相同时若比较 Forecast 对象会 TypeError。
        visible = sorted(
            (
                (guard.visible_from(f.pub_date, KIND_EXPRESS_REPORT).date(), f)
                for f in forecasts_by_symbol.get(symbol, ())
            ),
            key=lambda pair: pair[0],
        )

        new_bars: list[Bar] = []
        j = 0
        active: Forecast | None = None
        active_idx = -1

        for i, bar in enumerate(ordered):
            d = bar.dt.date()
            while j < len(visible) and visible[j][0] <= d:
                active = visible[j][1]
                active_idx = i  # 该预告首次可见的 Bar 下标
                j += 1

            if active is None or i - active_idx > max_age_bars:
                new_bars.append(bar)
                continue

            new_bars.append(
                replace(
                    bar,
                    fc_score=forecast_kind_score(active.kind),
                    fc_chg_pct=active.chg_pct_mid,
                    fc_age_days=float(i - active_idx),
                )
            )
        out[symbol] = new_bars
    return out


# baostock 财务字段 → Bar 字段
FIN_FIELD_MAP: dict[str, str] = {
    "roeAvg": "fin_roe",
    "gpMargin": "fin_gp_margin",
    "npMargin": "fin_np_margin",
    "AssetTurnRatio": "fin_asset_turn",
    "liabilityToAsset": "fin_liab_to_asset",
    "YOYNI": "fin_yoy_ni",
    "CFOToOR": "fin_cfo_to_or",
    "dupontROE": "fin_dupont_roe",
}


def _fin_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def attach_financials(
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    financials_by_symbol: Mapping[str, Mapping[date, Mapping[str, Any]]],
    *,
    pit: PITGuard | None = None,
    max_age_bars: int = 250,
) -> dict[str, list[Bar]]:
    """把「**截至该 Bar 当日已公布**」的最新财报附加到 Bar 上。

    ## PIT 规则

    **优先用数据源给的 ``pubDate``（实际公告日）+ 1 天**，而不是「报告期 + 法定滞后」。
    理由：法定滞后是**没有公告日时的保守替代**；既然有真实公告日，用它更准确
    （且仍 +1 天保守）。缺 ``pubDate`` 时才退回 ``statDate + FINANCIAL_LAG_DAYS``。

    ## 信息衰减

    ``max_age_bars=250``（约一年）后置 ``None``。财报是**季度**信息，
    超过 4 个季度不更新说明该标的已无有效财报（或长期停牌）。

    > ⚠️ **重要**：财报因子值在两次公告之间**完全不变**。故**不能用日频截面 IC**
    > 评估 —— 那会把同一份财报重复计入 ~60 次，制造严重自相关。
    > 正确做法见 ``evaluation.xsec.build_report_sections``（按报告期采样）。
    """
    guard = pit or PITGuard()
    out: dict[str, list[Bar]] = {}

    for symbol, bars in bars_by_symbol.items():
        ordered = sorted(bars, key=lambda b: b.dt)
        if not ordered:
            out[symbol] = []
            continue

        reports: list[tuple[date, dict[str, Any]]] = []
        for stat_date, fields in (financials_by_symbol.get(symbol) or {}).items():
            pub_raw = fields.get("pubDate")
            pub = None
            if pub_raw:
                try:
                    pub = date.fromisoformat(str(pub_raw)[:10])
                except ValueError:
                    pub = None
            if pub is not None:
                visible = pub + timedelta(days=1)  # 公告日次日
            else:
                # 无公告日 → 退回「报告期 + 法定滞后」
                lag = FINANCIAL_LAG_DAYS.get(_quarter_key(stat_date), 120)
                visible = stat_date + timedelta(days=lag)
            reports.append((visible, fields))
        reports.sort(key=lambda pair: pair[0])

        new_bars: list[Bar] = []
        j = 0
        active: dict[str, Any] | None = None
        active_idx = -1
        for i, bar in enumerate(ordered):
            d = bar.dt.date()
            while j < len(reports) and reports[j][0] <= d:
                active = reports[j][1]
                active_idx = i
                j += 1
            if active is None or i - active_idx > max_age_bars:
                new_bars.append(bar)
                continue
            kwargs = {
                bar_field: _fin_float(active.get(bs_field))
                for bs_field, bar_field in FIN_FIELD_MAP.items()
            }
            new_bars.append(replace(bar, **kwargs))
        out[symbol] = new_bars
    return out


def _quarter_key(stat_date: date) -> str:
    """报告期 → 法定滞后键（q1/h1/q3/annual）。"""
    if stat_date.month == 3:
        return "q1"
    if stat_date.month == 6:
        return "h1"
    if stat_date.month == 9:
        return "q3"
    return "annual"


# 指数名 → Bar 字段
SHIPPING_FIELD_MAP: dict[str, str] = {
    "BDTI": "ship_bdt",
    "BDI": "ship_bdi",
    "BCTI": "ship_bct",
}


def attach_shipping(
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    indices: Mapping[str, Sequence[tuple[date, float]]],
    *,
    pit: PITGuard | None = None,
    lag_days: int = 2,
) -> dict[str, list[Bar]]:
    """把航运运价指数附加到 Bar 上。

    ## PIT 规则

    数据源只给 ``REPORT_DATE``（**数据日期**），不给发布日。保守取
    **数据日期 + ``lag_days``（默认 2 天）** 为可见时点。

    理由：BDTI 由波罗的海交易所于伦敦时间当日发布，东财转载有延迟；
    且 A 股开盘早于伦敦，**T 日的 BDTI 不可能在 T 日 A 股开盘前被用到**。

    > ⚠️ **不做插值**：运价指数是交易日频（周一至周五），与 A 股交易日历
    > 不完全一致。缺失日**沿用上一个已知值**（carry-forward），而不是插值——
    > 插值会引入未来信息。
    """
    guard = pit or PITGuard()
    # 每个指数 → 按可见日排序的 (可见日, 值)
    prepared: dict[str, list[tuple[date, float]]] = {}
    for name, series in indices.items():
        if name not in SHIPPING_FIELD_MAP:
            continue
        prepared[name] = sorted(
            (d + timedelta(days=lag_days), v) for d, v in series
        )

    out: dict[str, list[Bar]] = {}
    for symbol, bars in bars_by_symbol.items():
        ordered = sorted(bars, key=lambda b: b.dt)
        ptrs = {name: 0 for name in prepared}
        latest: dict[str, float | None] = {name: None for name in prepared}

        new_bars: list[Bar] = []
        for bar in ordered:
            d = bar.dt.date()
            for name, series in prepared.items():
                i = ptrs[name]
                while i < len(series) and series[i][0] <= d:
                    latest[name] = series[i][1]
                    i += 1
                ptrs[name] = i
            kwargs = {
                SHIPPING_FIELD_MAP[name]: latest[name] for name in prepared
            }
            new_bars.append(replace(bar, **kwargs) if kwargs else bar)
        out[symbol] = new_bars
    return out


def build_return_panel(
    bars_by_symbol: Mapping[str, Sequence[Bar]],
    horizon: int,
    *,
    entry_at_open: bool = True,
) -> Panel:
    """构建 ``{日期: {标的: 前向收益}}``。

    日期为**信号日 t**；收益为 ``t+1 开盘 → t+h 收盘``。
    """
    panel: Panel = {}
    for symbol, bars in bars_by_symbol.items():
        ordered = sorted(bars, key=lambda b: b.dt)
        for i, bar in enumerate(ordered):
            if bar.is_suspended:
                continue
            r = forward_return(ordered, i, horizon, entry_at_open=entry_at_open)
            if r is None:
                continue
            panel.setdefault(bar.dt.date(), {})[symbol] = r
    return panel


def load_bars_by_symbol(
    provider,
    symbols: Sequence[str],
    start: date,
    end: date,
    *,
    adjust: AdjustFlag = AdjustFlag.HFQ,
) -> dict[str, list[Bar]]:
    """从数据源批量读取 K 线。因子用后复权，收益用不复权（由调用方决定）。"""
    out: dict[str, list[Bar]] = {}
    for symbol in symbols:
        bars = provider.get_bars(symbol, "1d", start, end, adjust=adjust)
        if bars:
            out[symbol] = list(bars)
    return out
