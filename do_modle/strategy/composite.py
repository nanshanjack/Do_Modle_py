"""多因子合成 —— 把多个弱因子拼成一个更强的信号。

## 为什么可能有效

单个因子 `IC_IR ≈ 0.1`，但若多个因子**方向稳健且彼此低相关**，
合成后的 IC_IR 可显著高于单个：

    IC_IR_combo ≈ IC_IR_single × √(K / (1 + (K−1)·ρ̄))

其中 ``ρ̄`` 是因子间平均相关。``ρ̄=0`` 时提升 ``√K`` 倍；``ρ̄=1`` 时无提升。

**航运板块的因子普遍高相关**（同行业、同周期），故 ``ρ̄`` 大，提升有限 —— 这正是要实测的。

## 两个纪律

1. **横截面标准化**：不同因子量纲差异巨大（EP≈0.05，mom≈0.2），
   必须先按**当日横截面**做 z-score / 排名，否则量纲大的因子会主导。
2. **不做参数拟合**：本模块只提供**等权**合成。
   加权方案（IC 加权、最优化权重）会在同一样本上拟合 → 过拟合。
   若要用，必须走 Walk-Forward 权重。

## PIT 安全

横截面标准化只用**当日**的截面数据，不涉及时间维度 → 无前视。
"""

from __future__ import annotations

import math
from typing import Mapping, Sequence

__all__ = [
    "Panel",
    "zscore_cross_section",
    "rank_cross_section",
    "combine_panels",
    "combine_ranks",
    "panel_correlation",
]

Panel = dict  # {date: {symbol: float}}


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def zscore_cross_section(panel: Panel) -> Panel:
    """按**日**做横截面 z-score。

    单期标的数 < 2 或标准差为 0 时，该期全部置 0（无信息）。
    """
    out: Panel = {}
    for d, per_symbol in panel.items():
        vals = list(per_symbol.values())
        if len(vals) < 2:
            continue
        m, s = _mean(vals), _std(vals)
        if s == 0:
            out[d] = {sym: 0.0 for sym in per_symbol}
            continue
        out[d] = {sym: (v - m) / s for sym, v in per_symbol.items()}
    return out


def rank_cross_section(panel: Panel) -> Panel:
    """按**日**做横截面百分位排名（0~1）。对异常值稳健。"""
    out: Panel = {}
    for d, per_symbol in panel.items():
        if len(per_symbol) < 2:
            continue
        syms = sorted(per_symbol, key=lambda s: per_symbol[s])
        n = len(syms)
        out[d] = {sym: i / (n - 1) for i, sym in enumerate(syms)}
    return out


def combine_panels(
    panels: Mapping[str, Panel],
    weights: Mapping[str, float] | None = None,
    *,
    method: str = "zscore",
) -> Panel:
    """把多个因子面板合成为单一面板。

    ``method``：

    * ``"zscore"`` —— 先横截面 z-score 再加权求和（默认）
    * ``"rank"``   —— 先横截面百分位排名再加权求和

    ``weights`` 为 ``None`` 时**等权**（不做任何拟合）。

    **缺失处理**：某标的在某日只有部分因子有值 → 只用有值的因子，
    并按可用权重归一化。这样不会因个别字段缺失而丢掉整个标的。
    """
    if not panels:
        return {}
    names = list(panels)
    if weights is None:
        w = {n: 1.0 for n in names}
    else:
        missing = [n for n in names if n not in weights]
        if missing:
            raise KeyError(f"weights 缺少因子: {missing}")
        w = dict(weights)

    prep = zscore_cross_section if method == "zscore" else rank_cross_section
    if method not in ("zscore", "rank"):
        raise ValueError(f"未知 method: {method!r}，可选 'zscore' / 'rank'")

    prepared = {n: prep(panels[n]) for n in names}

    dates: set = set()
    for n in names:
        dates |= set(prepared[n])
    out: Panel = {}
    for d in sorted(dates):
        acc: dict[str, float] = {}
        wsum: dict[str, float] = {}
        for n in names:
            per = prepared[n].get(d)
            if not per:
                continue
            for sym, v in per.items():
                acc[sym] = acc.get(sym, 0.0) + w[n] * v
                wsum[sym] = wsum.get(sym, 0.0) + w[n]
        row = {sym: acc[sym] / wsum[sym] for sym in acc if wsum[sym] > 0}
        if len(row) >= 2:
            out[d] = row
    return out


def combine_ranks(panels: Mapping[str, Panel], weights=None) -> Panel:
    """``combine_panels(method="rank")`` 的便捷入口。"""
    return combine_panels(panels, weights, method="rank")


def panel_correlation(a: Panel, b: Panel) -> float:
    """两个因子面板的**逐期横截面秩相关**均值 —— 判断能否分散。

    含义：同一时点上，两个因子对标的的排序有多像。

    * 相关低 → 可分散 → 合成收益大
    * 相关高（如两个都是动量）→ 合成几乎无增益

    航运板块的因子普遍高相关（同行业、同周期），故实际增益可能有限。
    """
    from do_modle.numeric import spearman

    corrs = []
    for d in sorted(set(a) & set(b)):
        common = sorted(set(a[d]) & set(b[d]))
        if len(common) < 3:
            continue
        corrs.append(spearman([a[d][s] for s in common], [b[d][s] for s in common]))
    valid = [c for c in corrs if c == c]
    return _mean(valid) if valid else float("nan")
