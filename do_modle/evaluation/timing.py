"""择时信号评估 —— 适用于**全行业共同指标**（运价、宏观、日历季节性）。

## 为什么需要单独的模块

`xsec.py` 的截面 IC 回答的是「**哪些标的**未来会涨」——
要求信号在**同一天的截面上有变异**。

但有一类信号在截面上**零变异**：

| 信号 | 同日各标的值 |
|---|---|
| BDTI 运价指数 | **完全相同**（全行业共同） |
| BDI / BCTI / BCR 等 | 完全相同 |
| 月份 / 日历季节性 | 完全相同 |
| 宏观指标（CPI/PPI/PMI） | 完全相同 |

对这些信号算截面 IC **恒为 nan**（实测验证：同日 6 只标的的 BDTI 取值个数 = 1）。

## 正确的方法：分层择时

阶段一是**单标的择时策略**（只做 601872），要回答的是
「**现在该不该持有**」，而不是「买哪只」。

评估方法：

1. 按信号值分位分组（如 5 档）
2. 计算各组在**未来 N 日**的平均收益
3. 检验**单调性**（高信号组是否真的收益更高）
4. 检验**顶底差**的显著性

> ⚠️ **重叠窗口问题**：未来 N 日收益在相邻观测间重叠 → 自相关 → t 值虚高。
> 本模块用 ``non_overlapping`` 参数控制：默认按 N 日**不重叠采样**。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from do_modle.numeric import mean as _mean
from do_modle.numeric import std as _std
from do_modle.objects import Bar

__all__ = ["TimingResult", "quantile_timing"]


@dataclass
class TimingResult:
    """分层择时结果。"""

    n_groups: int
    horizon: int
    n_obs: int
    group_returns: list[float]  # 各组的平均未来收益（升序组）
    group_counts: list[int]
    spread: float  # 最高组 − 最低组
    spread_t: float  # 顶底差的 t 值
    monotonic: bool  # 各组收益是否单调递增
    n_non_overlapping: int  # 不重叠采样后的观测数

    def format_text(self, name: str = "") -> str:
        head = f"[{name}] " if name else ""
        lines = [
            f"{head}观测 {self.n_obs} 个（不重叠采样 {self.n_non_overlapping} 个）  "
            f"前向 {self.horizon} 日  分 {self.n_groups} 组",
        ]
        for i, (r, c) in enumerate(zip(self.group_returns, self.group_counts)):
            lines.append(f"    第 {i + 1} 组（n={c:>4}）  平均收益 {r:+.4%}")
        lines.append(
            f"    顶底差 {self.spread:+.4%}   t={self.spread_t:+.2f}   "
            f"单调性 {'✅ 单调' if self.monotonic else '❌ 非单调'}"
        )
        return "\n".join(lines)


def _quantile(sorted_vals: Sequence[float], v: float, n_groups: int) -> int:
    """返回 ``v`` 在 ``sorted_vals`` 中的分位组号（0 起）。"""
    n = len(sorted_vals)
    if n == 0:
        return -1
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_vals[mid] < v:
            lo = mid + 1
        else:
            hi = mid
    return min(int(lo / n * n_groups), n_groups - 1)


def quantile_timing(
    bars: Sequence[Bar],
    signal: Sequence[float | None],
    *,
    horizon: int = 20,
    n_groups: int = 5,
    non_overlapping: bool = True,
    min_obs: int = 20,
) -> TimingResult | None:
    """按信号分位分组，比较各组的**未来 N 日收益**。

    :param bars: 单标的的 Bar 序列（升序）
    :param signal: 与 ``bars`` 等长的信号序列（``None`` 表示该日无信号）
    :param horizon: 前向收益天数
    :param n_groups: 分位组数
    :param non_overlapping: **默认 True** —— 每 ``horizon`` 日只取一个观测，
        避免重叠窗口导致的 t 值虚高
    :param min_obs: 最少观测数，不足返回 ``None``

    返回 ``None`` 表示数据不足。
    """
    if len(bars) != len(signal):
        raise ValueError(
            f"bars 与 signal 长度不一致：{len(bars)} vs {len(signal)}"
        )
    if horizon < 1 or n_groups < 2:
        raise ValueError(f"参数非法：horizon={horizon}, n_groups={n_groups}")

    # 收集 (信号, 未来收益) 对
    pairs: list[tuple[date, float, float]] = []
    for i, (b, s) in enumerate(zip(bars, signal)):
        if s is None or s != s:
            continue
        j = i + horizon
        if j >= len(bars):
            break
        entry, exit_ = bars[i + 1].open, bars[j].close
        if entry <= 0:
            continue
        pairs.append((b.dt.date(), s, exit_ / entry - 1.0))

    if len(pairs) < min_obs:
        return None

    # 不重叠采样：按 horizon 天间隔取
    if non_overlapping:
        picked: list[tuple[date, float, float]] = []
        last: date | None = None
        for d, s, r in pairs:
            if last is None or (d - last).days >= horizon:
                picked.append((d, s, r))
                last = d
        pairs = picked
        if len(pairs) < min_obs:
            return None

    vals = sorted(s for _, s, _ in pairs)
    groups: list[list[float]] = [[] for _ in range(n_groups)]
    for _, s, r in pairs:
        g = _quantile(vals, s, n_groups)
        if g >= 0:
            groups[g].append(r)

    group_returns = [_mean(g) if g else float("nan") for g in groups]
    counts = [len(g) for g in groups]
    top, bottom = group_returns[-1], group_returns[0]

    if top != top or bottom != bottom:
        spread, spread_t = float("nan"), float("nan")
    else:
        spread = top - bottom
        # 顶底两组的合并标准误
        g_top, g_bot = groups[-1], groups[0]
        if len(g_top) >= 2 and len(g_bot) >= 2:
            se = math.sqrt(
                _std(g_top) ** 2 / len(g_top) + _std(g_bot) ** 2 / len(g_bot)
            )
            spread_t = spread / se if se > 0 else float("nan")
        else:
            spread_t = float("nan")

    valid = [r for r in group_returns if r == r]
    if len(valid) == n_groups:
        inc = all(group_returns[i] <= group_returns[i + 1] + 1e-12 for i in range(n_groups - 1))
        dec = all(group_returns[i] >= group_returns[i + 1] - 1e-12 for i in range(n_groups - 1))
        monotonic = inc or dec
    else:
        monotonic = False

    return TimingResult(
        n_groups=n_groups,
        horizon=horizon,
        n_obs=len(pairs),
        group_returns=group_returns,
        group_counts=counts,
        spread=spread,
        spread_t=spread_t,
        monotonic=monotonic,
        n_non_overlapping=len(pairs),
    )
